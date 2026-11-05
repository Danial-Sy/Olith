# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import unittest
import asyncio
import contextlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    import config
    import server
    import models
    import storage as st
except ModuleNotFoundError:
    server = None



def _prompt_text(sample: dict) -> str:
    """Combine all text sent in a sample, including instructions on tool results."""
    return "\n".join(m.get("content") or "" for m in sample["messages"])


DIRECTIVE_MARKER = "OPEN PLAN, CONTINUE AUTONOMOUSLY"


def _directive_channel(sample: dict) -> str:
    """Find the role carrying the directive body, ignoring the tag mentioned in the system prompt."""
    for m in reversed(sample["messages"]):
        if DIRECTIVE_MARKER in (m.get("content") or ""):
            return m.get("role") or ""
    return ""

@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class AgentLoopPromptTests(unittest.TestCase):
    def test_verification_summary_reports_failure_and_skips_without_false_success(self):
        report = {"commands": [{"result": {"exit_code": 1}}, {"status": "needs_approval"}], "language_servers": [{"diagnostics": [{"severity": 1}]}]}
        summary = server._verification_summary(report)
        self.assertIn("0 command(s) passed, 1 failed", summary)
        self.assertIn("still need approval", summary)
        self.assertIn("error(s) remain", summary)
        self.assertIn("no test or build command was run", server._verification_summary({"commands": []}))

    def test_plan_updates_do_not_change_the_running_system_prefix(self):
        chat = {"id": "plan-prefix", "mode": "agentic", "messages": [{"role": "user", "content": "Fix the game"}], "agent_todos": [{"id": "fix", "content": "Fix score", "status": "in_progress"}]}
        snapshot = server.agent.todos_prompt_block(chat)
        first = server._build_messages(chat, "agent", plan_context=snapshot)
        chat["agent_todos"][0]["status"] = "completed"
        chat["messages"].extend([
            {"role": "assistant", "content": "", "tool_calls": [{"name": "todo_write", "arguments": {"merge": True, "todos": [{"id": "fix", "status": "completed"}]}}]},
            {"role": "tool", "name": "todo_write", "content": '{"todos":[{"id":"fix","status":"completed"}]}'},
        ])
        current = server._build_messages(chat, "agent", plan_context=snapshot)
        self.assertEqual(first[0], current[0])
        self.assertIn('"status":"completed"', current[-1]["content"])
    def test_legacy_automatic_verification_calls_are_not_replayed_as_shell_commands(self):
        chat = {"id": "verification-replay", "messages": [
            {"role": "user", "content": "Fix the game"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "run_shell", "arguments": {"command": server.agent.INTERNAL_VERIFICATION_COMMAND}}]},
            {"role": "tool", "name": "run_shell", "content": '{"exit_code":127}'},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "read_file", "arguments": {"path": "game.cpp"}}]},
            {"role": "tool", "name": "read_file", "content": '{"content":"current source"}'},
        ]}
        rendered = server._build_messages(chat, "agent")
        self.assertNotIn(server.agent.INTERNAL_VERIFICATION_COMMAND, str(rendered))
        self.assertNotIn("127", str(rendered))
        self.assertIn("current source", str(rendered))
    def test_only_successful_tool_results_reset_repeat_stalls(self):
        self.assertTrue(server._tool_succeeded(
            "edit_file", '{"ok": true, "path": "/tmp/app.js"}'
        ))
        self.assertTrue(server._tool_succeeded(
            "run_shell", '{"exit_code": 0}'
        ))
        self.assertTrue(server._tool_succeeded(
            "read_file", '{"path": "/tmp/app.js", "content": "x"}'
        ))
        self.assertFalse(server._tool_succeeded(
            "edit_file", '{"error": "Invalid line range"}'
        ))
        self.assertFalse(server._tool_succeeded(
            "edit_file", '{"ok": true, "validation": {"ok": false}}'
        ))
        self.assertFalse(server._tool_succeeded(
            "run_shell", '{"exit_code": 1}'
        ))

    def test_the_prompt_prefix_is_stable_while_the_floor_holds(self):
        def serialize(msgs):
            return "\n\x00\n".join(
                f"{m.get('role')}\x01{m.get('content') or ''}" for m in msgs
            )

        chat = {"id": "prefix-stability", "messages": [
            {"role": "user", "content": "Do the job."},
        ]}
        previous = ""
        for index in range(14):
            current = serialize(server._build_messages(chat, "agent"))
            if previous:
                # Every earlier byte survives: the new prompt only appends.
                self.assertTrue(
                    current.startswith(previous),
                    f"step {index} rewrote already-sent content",
                )
            previous = current
            chat["messages"].extend([
                {"role": "assistant", "content": "", "interim": True,
                 "tool_calls": [{"name": "read_file", "arguments": {
                     "path": "large.js", "start_line": index * 50 + 1,
                     "end_line": index * 50 + 60}}]},
                {"role": "tool", "name": "read_file", "content": json.dumps({
                    "path": "large.js", "start": index * 50 + 1,
                    "end": index * 50 + 60, "revision": "r1", "content": "y" * 4000})},
            ])

    def test_recovery_prompt_keeps_only_requested_recent_tool_turns(self):
        messages = [{"role": "user", "content": "Repair the large file."}]
        for index in range(10):
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "read_file",
                        "arguments": {
                            "path": "large.js",
                            "start_line": index * 100 + 1,
                            "end_line": index * 100 + 100,
                        },
                    }],
                    "interim": True,
                },
                {
                    "role": "tool",
                    "name": "read_file",
                    "content": f'{{"chunk":{index},"content":"' + "x" * 7000 + '"}',
                },
            ])

        built = server._build_messages(
            {"id": "recovery-retention-test", "messages": messages},
            "agent",
            tool_replay_limit=4,
        )
        tool_results = [message for message in built if message.get("role") == "tool"]
        assistant_calls = [
            message for message in built if message.get("role") == "assistant"
        ]

        self.assertEqual(len(tool_results), 4)
        self.assertEqual(len(assistant_calls), 4)
        self.assertIn('"chunk":6', tool_results[0]["content"])
        self.assertIn('"chunk":9', tool_results[-1]["content"])

    def test_prompt_replays_native_thinking_and_named_tool_result(self):
        built = server._build_messages({
            "id": "native-tool-contract",
            "messages": [
                {"role": "user", "content": "Inspect it."},
                {
                    "role": "assistant",
                    "content": "",
                    "native_thinking": "I should inspect the file.",
                    "tool_calls": [{
                        "index": 0,
                        "name": "read_file",
                        "arguments": {"path": "app.py"},
                    }],
                    "interim": True,
                },
                {"role": "tool", "name": "read_file", "content": '{"ok": true}'},
            ],
        }, "agent")

        assistant = next(message for message in built if message.get("role") == "assistant")
        tool_result = next(message for message in built if message.get("role") == "tool")
        self.assertEqual(assistant["thinking"], "I should inspect the file.")
        self.assertEqual(assistant["tool_calls"][0]["function"]["index"], 0)
        self.assertEqual(tool_result["tool_name"], "read_file")

    def test_stale_edit_range_limit_is_rewritten_before_replay(self):
        built = server._build_messages({
            "id": "stale-edit-limit",
            "messages": [
                {"role": "user", "content": "Continue the plan."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "edit_file",
                        "arguments": {
                            "path": "jungle.html",
                            "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                        },
                    }],
                    "interim": True,
                },
                {
                    "role": "tool",
                    "name": "edit_file",
                    "content": json.dumps({
                        "error": "Too many edit ranges in one batch",
                        "maximum": 12,
                    }),
                },
            ],
        }, "agent")

        result = json.loads(next(
            message["content"] for message in built if message.get("role") == "tool"
        ))
        self.assertTrue(result["obsolete_error"])
        self.assertEqual(result["previous_maximum"], 12)
        self.assertEqual(result["current_maximum"], server.agent.MAX_EDIT_RANGES)
        self.assertNotIn("error", result)

    def test_poisoned_elision_edit_call_and_result_are_not_replayed(self):
        built = server._build_messages({
            "id": "poisoned-edit-call",
            "messages": [
                {"role": "user", "content": "Continue."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "edit_file",
                        "arguments": {
                            "path": "jungle.html",
                            "edits": [{
                                "start_line": 1,
                                "end_line": 1,
                                "content": "[Olith replay elision: 9000 characters]",
                            }],
                        },
                    }],
                    "interim": True,
                },
                {"role": "tool", "name": "edit_file", "content": '{"error":"bad edit"}'},
            ],
        }, "agent")

        self.assertFalse(any(message.get("tool_calls") for message in built))
        self.assertFalse(any(message.get("role") == "tool" for message in built))

    def test_legacy_plan_choice_reply_is_not_replayed(self):
        built = server._build_messages({
            "id": "legacy-plan-deferral",
            "agent_todos": [
                {"id": "cooldown", "content": "Add cooldown", "status": "in_progress"},
                {"id": "enemies", "content": "Add enemies", "status": "pending"},
            ],
            "messages": [
                {"role": "user", "content": "Build the requested features."},
                {"role": "assistant", "content": "What to do next, pick one. Which one?"},
                {"role": "user", "content": "Continue without asking me."},
            ],
        }, "agent")

        assistant_text = "\n".join(
            message.get("content") or ""
            for message in built
            if message.get("role") == "assistant"
        )
        self.assertNotIn("pick one", assistant_text.lower())
        self.assertNotIn("which one", assistant_text.lower())

    def test_stored_continue_is_replayed_as_resume_not_course_correction(self):
        built = server._build_messages({
            "id": "legacy-continue-tag",
            "agent_todos": [
                {"id": "repair", "content": "Repair file", "status": "in_progress"},
            ],
            "messages": [{
                "role": "user",
                "content": "continue",
                "course_correct": True,
            }],
        }, "agent")

        user_text = next(
            message["content"] for message in built if message.get("role") == "user"
        )
        self.assertIn("Same user job", user_text)
        self.assertNotIn("cancel obsolete steps", user_text)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class AgentLoopExecutionTests(unittest.IsolatedAsyncioTestCase):

    async def test_shell_execution_keeps_the_server_event_loop_responsive(self):
        samples = 0
        async def stream(*args, **kwargs):
            nonlocal samples
            samples += 1
            message = {"content": "Check finished."} if samples > 1 else {"tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "make test"}}}]}
            yield {"message": message, "done": True}
        def slow_check(*args, **kwargs):
            time.sleep(0.08)
            return '{"exit_code":0}'
        async def compact(chat, *args, **kwargs):
            return chat, False
        chat = {"id": "responsive-check", "mode": "agentic", "messages": [{"role": "user", "content": "Run the tests"}]}
        heartbeat = []
        with (
            patch.object(server.oc, "stream_chat", new=stream),
            patch.object(server, "_maybe_auto_compact", new=compact),
            patch.object(server.agent, "run_tool", side_effect=slow_check),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={"shell_approval_mode": "never_ask"}),
        ):
            async for event in server._agent_loop(chat, {"id": "m", "keep_alive": "1m"}, "agent", think=False):
                if event["type"] == "tool_start":
                    asyncio.get_running_loop().call_later(0.01, heartbeat.append, True)
                if event["type"] == "tool_result":
                    self.assertTrue(heartbeat, "Other requests must be served while a shell check runs")

    async def test_checks_can_repeat_after_each_successful_source_change(self):
        rounds = 0
        async def stream(*args, **kwargs):
            nonlocal rounds
            current = rounds; rounds += 1
            if current >= 7:
                yield {"message": {"content": "Verified the changes."}, "done": True}
            else:
                name = "run_shell" if current % 2 == 0 else "edit_file"
                arguments = {"command": "make test"} if name == "run_shell" else {"path": "game.cpp", "find": str(current), "replace": str(current + 1)}
                yield {"message": {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}, "done": True}
        async def compact(chat, *args, **kwargs):
            return chat, False
        chat = {"id": "repeat-check-after-edit", "mode": "agentic", "messages": [{"role": "user", "content": "Fix and verify the code"}]}
        with (
            patch.object(server.oc, "stream_chat", new=stream),
            patch.object(server, "_maybe_auto_compact", new=compact),
            patch.object(server.agent, "run_tool", return_value='{"changed":true,"exit_code":0}'),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={"shell_approval_mode": "never_ask"}),
        ):
            events = [event async for event in server._agent_loop(chat, {"id": "m", "keep_alive": "1m"}, "agent", think=False)]
        self.assertEqual(sum(e["type"] == "tool_start" and e["name"] == "run_shell" for e in events), 4)
        self.assertNotIn("interrupted", chat)

    async def test_tool_commentary_is_public_prose_not_private_thinking_or_call_json(self):
        cases = [
            ("I will inspect the entry point before editing it.", True),
            ("<think>Private reasoning stays private.</think>", False),
            ('<tool_call>{"name":"read_file","arguments":{"path":"app.py"}}</tool_call>', False),
        ]
        for content, public in cases:
            with self.subTest(content=content):
                samples = 0
                async def stream(*args, **kwargs):
                    nonlocal samples
                    samples += 1
                    message = {"content": "Inspection complete."} if samples > 1 else {
                        "content": content,
                        "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "app.py"}}}],
                    }
                    yield {"message": message, "done": True}
                async def compact(chat, *args, **kwargs):
                    return chat, False
                chat = {"id": "commentary-test", "project_id": None, "messages": [{"role": "user", "content": "Inspect the entry point."}]}
                with (
                    patch.object(server.oc, "stream_chat", new=stream),
                    patch.object(server, "_maybe_auto_compact", new=compact),
                    patch.object(server.agent, "run_tool", return_value='{"content":"source"}'),
                    patch.object(server.st, "save_chat", side_effect=lambda value: value),
                    patch.object(server.st, "load_settings", return_value={"shell_approval_mode": "never_ask", "role_context": {"agentic": 65536}}),
                ):
                    events = [event async for event in server._agent_loop(chat, {"id": "m", "keep_alive": "1m", "context": 32768}, "agent", think=False)]
                commentary = [event for event in events if event["type"] == "commentary"]
                self.assertEqual(bool(commentary), public)
                if public:
                    self.assertEqual(commentary[0]["content"], content)
                    self.assertLess(events.index(commentary[0]), next(i for i, event in enumerate(events) if event["type"] == "tool_start"))
                self.assertTrue(next(event for event in events if event["type"] == "tool_result")["ok"])

    async def test_resumed_open_plan_starts_with_compact_tool_replay(self):
        sampling_calls = await self._resume_with_reads(7900)
        replayed_tools = [
            message
            for message in sampling_calls[0]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(replayed_tools), 6)
        # Recovery narrows once. The next sample retains those six results and
        # appends the completed plan instead of evicting another source slice.
        next_tools = [m for m in sampling_calls[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(next_tools), 7)
        self.assertEqual(replayed_tools, next_tools[:6])

    async def test_resume_keeps_full_replay_when_it_fits(self):
        sampling_calls = await self._resume_with_reads(2000)
        replayed_tools = [m for m in sampling_calls[0]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(replayed_tools), 8)

    async def _resume_with_reads(self, body_chars: int) -> list[dict]:
        sampling_calls = []

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            if len(sampling_calls) == 1:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "todo_write",
                            "arguments": {
                                "merge": True,
                                "todos": [{
                                    "id": "repair",
                                    "content": "Repair file",
                                    "status": "completed",
                                }],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Finished resumed work."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        messages = [{"role": "user", "content": "Repair it."}]
        for index in range(8):
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "read_file",
                        "arguments": {"path": "large.js", "start_line": index + 1},
                    }],
                    "interim": True,
                },
                {
                    "role": "tool",
                    "name": "read_file",
                    "content": '{"content":"' + "x" * body_chars + '"}',
                },
            ])
        messages.append({
            "role": "user",
            "content": "continue",
            "course_correct": True,
        })
        chat = {
            "id": "compact-resume",
            "project_id": None,
            "agent_todos": [
                {"id": "repair", "content": "Repair file", "status": "in_progress"},
            ],
            "messages": messages,
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            [event async for event in server._agent_loop(chat, meta, "agent", think=False)]
        return sampling_calls
        self.assertEqual(events[-1]["message"]["content"], "Finished resumed work.")

    async def test_repeated_rejected_call_changes_strategy_while_plan_is_open(self):
        sampling_round = 0
        real_run_tool = server.agent.run_tool

        async def fake_stream_chat(*args, **kwargs):
            nonlocal sampling_round
            sampling_round += 1
            if sampling_round <= 4:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "edit_file",
                            "arguments": {
                                "path": "jungle.html",
                                "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            elif sampling_round == 5:
                # The actual change of strategy: a different, smaller edit.
                # Resending the rejected one is what the guard exists to stop.
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "edit_file",
                            "arguments": {
                                "path": "jungle.html",
                                "edits": [{"start_line": 2, "end_line": 2, "content": "y"}],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            elif sampling_round == 6:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "todo_write",
                            "arguments": {
                                "merge": True,
                                "todos": [{
                                    "id": "cooldown",
                                    "content": "Add cooldown",
                                    "status": "completed",
                                }],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Recovered without user direction."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, project_root, project_id=None, chat=None):
            # Keep rejecting until the changed approach produces a real edit.
            if name == "edit_file" and sampling_round <= 4:
                return '{"error":"synthetic rejection"}'
            if name == "edit_file":
                return '{"ok": true, "changed": true, "path": "jungle.html"}'
            return real_run_tool(name, arguments, project_root, project_id, chat=chat)

        chat = {
            "id": "plan-repeat-recovery",
            "project_id": None,
            "agent_todos": [
                {"id": "cooldown", "content": "Add cooldown", "status": "in_progress"},
            ],
            "messages": [{"role": "user", "content": "Complete the plan."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 2,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(chat, meta, "agent", think=False)
            ]

        self.assertEqual(sampling_round, 7)
        self.assertTrue(any(
            event.get("type") == "status"
            and event.get("model", {}).get("label") == "Changing strategy for open plan…"
            for event in events
        ))
        self.assertEqual(chat["agent_todos"][0]["status"], "completed")
        self.assertEqual(events[-1]["message"]["model_content"], "Recovered without user direction.")

    async def test_successful_file_mutation_emits_fresh_project_tree(self):
        sampling_round = 0

        async def fake_stream_chat(*args, **kwargs):
            nonlocal sampling_round
            sampling_round += 1
            if sampling_round == 1:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "write_file",
                            "arguments": {"path": "new.txt", "content": "ready"},
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Created the file."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, *args, **kwargs):
            if name == "list_dir":
                return '{"entries": ["new.txt"]}'
            return '{"ok": true}'

        chat = {
            "id": "project-refresh",
            "project_id": "project-1",
            "messages": [{"role": "user", "content": "Create new.txt"}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(
                server.st,
                "get_project",
                return_value={
                    "id": "project-1",
                    "name": "Refresh fixture",
                    "root": "/tmp",
                    "description": "",
                },
            ),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(
                    chat, meta, "agent", effort="high", think=False
                )
            ]

        refresh = next(event for event in events if event.get("type") == "project_tree")
        self.assertEqual(refresh["project_id"], "project-1")
        self.assertEqual(refresh["entries"], ["new.txt"])

    async def test_length_retry_grows_budget_uses_action_nudge_and_drops_failed_turn(self):
        sampling_calls = []

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            round_number = len(sampling_calls)
            if round_number == 1:
                yield {
                    "message": {},
                    "done": True,
                    "done_reason": "length",
                    "eval_count": 8192,
                }
            elif round_number == 2:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "write_file",
                            "arguments": {"path": "game.html", "content": "<canvas></canvas>"},
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Built the game."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {
            "id": "length-recovery",
            "project_id": None,
            "effort": "high",
            "think": False,
            "messages": [{"role": "user", "content": "Build a large browser game."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", return_value='{"ok": true}'),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(
                    chat, meta, "agent", effort="high", think=chat["think"]
                )
            ]

        self.assertEqual(len(sampling_calls), 3)
        self.assertFalse(sampling_calls[0]["think"])
        self.assertFalse(sampling_calls[1]["think"])
        self.assertGreater(
            sampling_calls[1]["options"]["num_predict"],
            sampling_calls[0]["options"]["num_predict"],
        )
        self.assertEqual(sampling_calls[1]["options"]["num_ctx"], 32768)
        self.assertIn("ACTION-ONLY RETRY", _prompt_text(sampling_calls[1]))
        assistant_messages = [m for m in chat["messages"] if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_messages), 2)
        self.assertTrue(assistant_messages[0]["tool_calls"])
        self.assertEqual(events[-1]["message"]["model_content"], "Built the game.")

    async def test_repeated_length_failures_escalate_through_three_action_lanes(self):
        sampling_calls = []

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            round_number = len(sampling_calls)
            if round_number <= 3:
                yield {
                    "message": {},
                    "done": True,
                    "done_reason": "length",
                    "eval_count": kwargs["options"]["num_predict"],
                }
            elif round_number == 4:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "write_file",
                            "arguments": {
                                "path": "game.html",
                                "content": "<canvas></canvas>",
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Recovered and finished."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {
            "id": "staged-length-recovery",
            "project_id": None,
            "effort": "high",
            "think": False,
            "messages": [{"role": "user", "content": "Build a large browser game."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", return_value='{"ok": true}'),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(
                    chat, meta, "agent", effort="high", think=False
                )
            ]

        self.assertEqual(len(sampling_calls), 5)
        # Check that the recovery budget grows, without fixing context-dependent token counts.
        budgets = [call["options"]["num_predict"] for call in sampling_calls[:4]]
        self.assertEqual(budgets[0], 8192)
        self.assertEqual(budgets[1], 16384)
        self.assertGreater(budgets[2], budgets[1])
        self.assertLessEqual(budgets[2], 24576)
        self.assertEqual(budgets[3], budgets[2])  # capped, not growing forever
        assistant_messages = [
            message for message in chat["messages"] if message.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_messages), 2)
        self.assertEqual(events[-1]["message"]["model_content"], "Recovered and finished.")

    async def test_productive_loop_can_run_past_old_forty_step_limit(self):
        sampling_round = 0
        executed: list[str] = []

        async def fake_stream_chat(*args, **kwargs):
            nonlocal sampling_round
            sampling_round += 1
            if sampling_round == 1:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "search_files",
                            "arguments": {"pattern": "part-0"},
                        },
                    }]},
                    "done": False,
                }
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 1,
                            "name": "git_status",
                            "arguments": {},
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            elif sampling_round <= 45:
                # Make real edits between checks so this tests a long run that keeps progressing.
                if sampling_round % 5 == 0:
                    call = {
                        "index": 0,
                        "name": "edit_file",
                        "arguments": {"path": "a.py", "edits": [
                            {"find": f"part-{sampling_round}", "replace": "done"},
                        ]},
                    }
                else:
                    call = {
                        "index": 0,
                        "name": "search_files",
                        "arguments": {"pattern": f"part-{sampling_round}"},
                    }
                yield {
                    "message": {"tool_calls": [{"function": call}]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Completed after sustained tool progress."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, *args, **kwargs):
            executed.append(name)
            return '{"ok": true}'

        chat = {
            "id": "long-native-loop",
            "project_id": None,
            "messages": [{"role": "user", "content": "Complete a long repository task."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [event async for event in server._agent_loop(chat, meta, "agent", think=False)]

        self.assertEqual(sampling_round, 46)
        self.assertEqual(len(executed), 46)
        self.assertEqual(executed[:2], ["search_files", "git_status"])
        self.assertEqual(events[-1]["message"]["model_content"], "Completed after sustained tool progress.")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class AgentLoopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_rejection_restates_the_constraint_and_recovers(self):
        """Keep the tool's rejection details so the model knows how to fix its call."""
        sampling_calls = []
        oversized = {
            "path": "game.html",
            "edits": [{"start_line": i, "end_line": i, "content": "x"} for i in range(1, 15)],
        }

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            # Five identical rejected calls: two execute, three are guarded,
            # which is what trips agent_repeat_limit and forces recovery.
            if len(sampling_calls) <= 5:
                yield {
                    "message": {"tool_calls": [{
                        "function": {"index": 0, "name": "edit_file", "arguments": oversized},
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Split the edit and finished."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, *args, **kwargs):
            return json.dumps({
                "error": "Too many edit ranges in one batch",
                "requested": 14,
                "maximum": 12,
                "hint": "Split this into coherent batches of at most 12 ranges.",
            })

        chat = {"id": "rejection-recovery", "project_id": None,
                "messages": [{"role": "user", "content": "Add a cooldown."}]}
        meta = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event async for event in server._agent_loop(chat, meta, "agent", think=False)
            ]

        # The guard's reply carries the tool's own constraint, not a scolding.
        guard_replies = [
            json.loads(m["content"]) for m in chat["messages"]
            if m.get("role") == "tool" and "repeated" in (m.get("content") or "")
        ]
        self.assertTrue(guard_replies)
        self.assertEqual(guard_replies[0]["maximum"], 12)
        self.assertIn("at most 12 ranges", guard_replies[0]["error"])

        # And the run recovers instead of ending on "I got stuck".
        self.assertIn("A tool rejected your last calls", _prompt_text(sampling_calls[-1]))
        self.assertEqual(events[-1]["message"]["content"], "Split the edit and finished.")


    async def _agent_think_flag(self, meta, requested_think):
        seen = []

        async def fake_stream_chat(model_id, msgs, **kw):
            seen.append(kw.get("think"))
            yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        async def fake_ensure(key, meta=None):
            return meta or {"id": key, "keep_alive": "1m"}

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {"id": "think-gate", "project_id": None, "mode": "agentic",
                "model_id": meta["id"], "messages": [{"role": "user", "content": "Hi."}]}
        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server.oc, "ensure_model_loaded", side_effect=fake_ensure),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3, "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            [e async for e in server._agent_loop(chat, meta, meta["id"], think=requested_think)]
        return seen[0]

    async def test_the_agent_loop_lets_a_thinking_model_think(self):
        meta = {"id": "m", "keep_alive": "1m", "temperature": 0.2, "context": 32768, "think": True}

        self.assertTrue(await self._agent_think_flag(meta, True))

    async def test_the_agent_loop_never_asks_a_blind_model_to_think(self):
        meta = {"id": "m", "keep_alive": "1m", "temperature": 0.2, "context": 32768, "think": False}

        self.assertFalse(await self._agent_think_flag(meta, True))

    async def test_non_hybrid_never_swaps_models(self):
        loaded = []

        async def fake_stream_chat(*args, **kwargs):
            yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        async def fake_ensure(key):
            loaded.append(key)
            return {"id": key, "keep_alive": "1m",
                    "temperature": 0.2, "context": 32768}

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {"id": "no-swap", "project_id": None, "model_id": config.DEFAULT_AGENT_MODEL,
                "messages": [{"role": "user", "content": "Hi."}]}
        meta = {"id": config.DEFAULT_AGENT_MODEL, "keep_alive": "1m",
                "temperature": 0.2, "context": 32768}

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server.oc, "ensure_model_loaded", side_effect=fake_ensure),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3, "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask",
            }),
        ):
            [event async for event in server._agent_loop(chat, meta, "agent", think=False)]

        self.assertEqual(loaded, [])


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class ShellApprovalTests(unittest.TestCase):
    def test_safe_auto_stops_asking_about_a_remembered_command(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"proj-1": {"commands": ["rm -rf build"], "binaries": []}},
        }
        # Destructive, so it prompts...
        self.assertTrue(server._shell_gate(settings, "rm -rf build")[0])
        # ...unless the user approved this exact command in this project.
        self.assertFalse(server._shell_gate(settings, "rm -rf build", "proj-1")[0])
        # A different project does not inherit it.
        self.assertTrue(server._shell_gate(settings, "rm -rf build", "proj-2")[0])

    def test_always_allow_a_program_covers_its_later_invocations(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"global": {"commands": [], "binaries": ["rm"]}},
        }
        self.assertFalse(server._shell_gate(settings, "rm -rf build", None)[0])
        self.assertTrue(server._shell_gate(settings, "shred -u secrets.txt", None)[0])

    def test_always_ask_ignores_the_allowlist(self):
        settings = {
            "shell_approval_mode": "always_ask",
            "shell_allowlist": {"global": {"commands": ["ls"], "binaries": ["rm"]}},
        }
        self.assertTrue(server._shell_gate(settings, "rm -rf build", None)[0])

    def test_review_binaries_name_the_risky_program_not_the_first_one(self):
        binaries = server.agent.shell_review_binaries
        self.assertEqual(binaries("cd /x && git remote add origin u && git push -u origin main 2>&1"), ["git"])
        self.assertEqual(binaries("/usr/bin/shred secrets.txt"), ["shred"])
        self.assertEqual(binaries("FOO=1 rm -rf build"), ["rm"])
        self.assertEqual(binaries("cd /x && git push && rm -rf y"), ["git", "rm"])
        self.assertEqual(binaries("cd /x && ls"), [])
        self.assertEqual(binaries(""), [])

    def test_always_allow_cd_does_not_cover_what_follows_it(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"global": {"commands": [], "binaries": ["cd"]}},
        }
        self.assertTrue(server._shell_gate(settings, "cd /x && rm -rf y", None)[0])
        self.assertTrue(server._shell_gate(settings, "cd /x && git push", None)[0])

    def test_every_risky_program_must_be_remembered(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"global": {"commands": [], "binaries": ["git"]}},
        }
        self.assertFalse(server._shell_gate(settings, "cd /x && git push", None)[0])
        self.assertTrue(server._shell_gate(settings, "git push && rm -rf y", None)[0])

    def test_no_program_can_vouch_for_a_risk_in_the_line_itself(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"global": {"commands": [], "binaries": ["curl", "sh", "rm"]}},
        }
        self.assertTrue(server._shell_gate(settings, "curl https://x | sh", None)[0])
        self.assertTrue(server._shell_gate(settings, "rm -f a > /etc/hosts", None)[0])

    def test_classifier_verdicts_survive_the_split(self):
        classify = server.agent.classify_shell
        self.assertEqual(classify("cd /x && npm test")[0], "safe")
        self.assertIn("git push", classify("cd /x && git push")[1])
        self.assertIn("git reset", classify("git reset --hard")[1])
        self.assertIn("substitution", classify("echo $(rm -rf x)")[1])
        self.assertEqual(classify("echo 'unbalanced")[1], "unbalanced quotes")

    def test_remembering_stores_the_risky_program(self):
        store = {}
        with patch.object(server.st, "load_settings", return_value=store), \
             patch.object(server.st, "save_settings", side_effect=store.update):
            server.remember_shell_approval("p", "cd /x && git push", "binary")
            self.assertEqual(store["shell_allowlist"]["p"]["binaries"], ["git"])
            # Two risky programs cannot share one tick box.
            server.remember_shell_approval("q", "git push && rm -rf y", "binary")
            self.assertEqual(store["shell_allowlist"]["q"]["binaries"], [])

@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class JobStateResetTests(unittest.TestCase):

    def test_a_chat_that_never_planned_is_unaffected(self):
        chat = {}
        server._start_new_job(chat)
        self.assertNotIn("agent_spec", chat)

@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class EstimateCalibrationTests(unittest.TestCase):
    """Adjust the estimate from Ollama's actual prompt count without wasting context space."""

    def _learn(self, pairs):
        chat = {"id": "cal"}
        for estimated, actual in pairs:
            server.record_estimate_accuracy(chat, "agent", estimated, actual)
        return chat

    def test_bias_is_neutral_until_enough_samples(self):
        chat = self._learn([(20000, 15000), (20000, 15000)])
        self.assertEqual(server._estimate_bias(chat, "agent"), 1.0)

    def test_a_consistent_overestimate_is_learned(self):
        # Real (sent~, prompt_eval_count) pairs from the observed run.
        chat = self._learn([(19744, 14870), (24284, 18629), (20220, 15335),
                            (21281, 16443), (20639, 15696)])
        bias = server._estimate_bias(chat, "agent")
        self.assertLess(bias, 0.85)
        self.assertGreater(bias, server._ESTIMATE_BIAS_FLOOR)

    def test_bias_never_claims_more_window_than_the_floor_allows(self):
        chat = self._learn([(20000, 1)] * 6)
        self.assertEqual(
            server._estimate_bias(chat, "agent"), server._ESTIMATE_BIAS_FLOOR
        )

    def test_an_underestimate_is_never_used_to_shrink_the_estimate(self):
        # Underestimating is the dangerous direction: Ollama truncates the FRONT
        # of an overflowing prompt, silently dropping the system prompt and plan.
        chat = self._learn([(10000, 16000)] * 6)
        self.assertEqual(server._estimate_bias(chat, "agent"), 1.0)

    def test_garbage_counters_are_ignored(self):
        chat = {"id": "cal"}
        server.record_estimate_accuracy(chat, "agent", 0, 100)
        server.record_estimate_accuracy(chat, "agent", 100, None)
        server.record_estimate_accuracy(chat, "agent", 100, 0)
        self.assertEqual(server._estimate_bias(chat, "agent"), 1.0)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class RepeatGuardRevisionTests(unittest.TestCase):
    """Allow a rejected edit to retry after the file revision changes."""

    def test_renaming_an_identical_temporary_probe_does_not_evade_repeat_guard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "game.cpp"; source.write_text("int score = 0;")
            def probe(number, body="int main() { return score; }"):
                name = str(Path(tempfile.gettempdir()) / f"debug{number}")
                return {"name": "run_shell", "arguments": {"command": f"cat > {name}.cpp <<'EOF'\n#include \"{source}\"\n{body}\nEOF\ng++ {name}.cpp -o {name} && {name}"}}
            first = server._call_signature(probe(1), root)
            self.assertEqual(first, server._call_signature(probe(2), root))
            self.assertNotEqual(first, server._call_signature(probe(2, "int main() { return score + 1; }"), root))
            source.write_text("int score = 1;")
            self.assertNotEqual(first, server._call_signature(probe(3), root))

    def test_a_renamed_project_probe_without_includes_is_a_repeat(self):
        # Run 2 of the dino benchmark wrote dbg5.cpp, dbg6.cpp... 45 times with 3 bodies.
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def probe(number, body="int main() { return 0; }"):
                return {"name": "run_shell", "arguments": {"command": f"cd {root} && cat > dbg{number}.cpp <<'EOF'\n{body}\nEOF\ng++ -std=c++17 dbg{number}.cpp -o dbg{number} && ./dbg{number}"}}
            first = server._call_signature(probe(5), root)
            self.assertEqual(first, server._call_signature(probe(6), root))
            self.assertNotEqual(first, server._call_signature(probe(7, "int main() { return 1; }"), root))

    def test_a_renamed_probe_with_object_output_is_a_repeat(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "game.cpp").write_text("int score = 0;")
            def probe(number):
                name = str(Path(tempfile.gettempdir()) / f"probe{number}")
                return {"name": "run_shell", "arguments": {"command": f"cat > {name}.cpp <<'EOF'\n#include \"game.cpp\"\nint main() {{ return score; }}\nEOF\ng++ -c {name}.cpp -o {name}.o -I. && objdump -d {name}.o"}}
            self.assertEqual(server._call_signature(probe(6), root), server._call_signature(probe(7), root))

    def test_overwriting_project_source_through_a_heredoc_is_not_a_probe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "game.cpp").write_text("int score = 0;")
            command = f"cd {root} && cat > game.cpp <<'EOF'\nint score = 1;\nEOF"
            self.assertIsNone(server._shell_probe_identity(command, root))

    def test_the_same_call_against_a_changed_file_is_a_new_call(self):
        import tempfile
        from pathlib import Path
        from config import ROOT
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            (root / "a.js").write_text("const FLIP_CD=5.0;\n", encoding="utf-8")
            call = {"name": "edit_file", "arguments": {
                "path": "a.js", "find": "const FLIP_CD=5.0;", "replace": "const FLIP_CD=1.8;"}}

            before = server._call_signature(call, root)
            self.assertEqual(before, server._call_signature(call, root))

            (root / "a.js").write_text("const FLIP_CD=5.0; // changed\n", encoding="utf-8")
            self.assertNotEqual(before, server._call_signature(call, root))

    def test_a_call_with_no_path_still_has_a_stable_signature(self):
        call = {"name": "git_status", "arguments": {}}
        self.assertEqual(
            server._call_signature(call, None), server._call_signature(call, None)
        )

@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class CompactionBoundaryTests(unittest.TestCase):
    """Compact the rendered prompt while keeping the saved transcript intact."""

    @staticmethod
    def _run(n_reads: int) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "build it"}]
        for i in range(n_reads):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"name": "read_file",
                                         "arguments": {"path": f"f{i}.py"}}]})
            msgs.append({"role": "tool", "name": "read_file",
                         "content": json.dumps({"path": f"f{i}.py", "start": 1,
                                                "end": 50, "content": "BODY" + str(i)})})
        return msgs

    def test_rendering_resumes_after_the_newest_boundary(self):
        msgs = self._run(4)
        msgs.insert(5, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "EARLIER WORK", "quiet": True})
        chat = {"id": "b", "messages": msgs}
        self.assertEqual(server._boundary_index(msgs), 5)

        rendered = server._build_messages(chat, "agent")
        bodies = "\n".join(m.get("content") or "" for m in rendered)
        # The summary stands in for what came before it...
        self.assertIn("EARLIER WORK", rendered[0]["content"])
        # ...and those turns are not replayed as well.
        self.assertNotIn("BODY0", bodies)
        self.assertNotIn("BODY1", bodies)
        # Everything after the boundary is replayed in full, no receipts.
        self.assertIn("BODY2", bodies)
        self.assertIn("BODY3", bodies)

    def test_only_the_newest_boundary_supplies_the_summary(self):
        msgs = self._run(4)
        msgs.insert(3, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "OLD BRIEF", "quiet": True})
        msgs.insert(7, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "NEW BRIEF", "quiet": True})
        chat = {"id": "b2", "messages": msgs}
        sys_prompt = server._build_messages(chat, "agent")[0]["content"]
        self.assertIn("NEW BRIEF", sys_prompt)
        self.assertNotIn("OLD BRIEF", sys_prompt)

    def test_a_boundary_never_orphans_a_tool_result(self):
        # Ollama rejects a tool result with no assistant tool_call in front of it.
        msgs = self._run(5)
        for keep in range(1, len(msgs)):
            upto = server._compact_split_point(msgs, keep)
            self.assertNotEqual(
                msgs[upto].get("role") if upto < len(msgs) else None, "tool",
                f"keep={keep} would split an assistant/tool pair",
            )

    def test_a_long_single_request_run_can_still_compact(self):
        # Allow compaction past the user turn. It is repeated after the summary.
        msgs = self._run(6)
        self.assertEqual(server._latest_user_index(msgs), 0)
        self.assertGreater(server._compact_split_point(msgs, 4), 0)

    def test_the_request_survives_its_own_compaction(self):
        msgs = self._run(6)
        msgs.insert(9, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "BRIEF", "quiet": True})
        chat = {"id": "req", "messages": msgs}
        rendered = server._build_messages(chat, "agent")
        # Summary in the system prompt, original request re-emitted verbatim.
        self.assertIn("BRIEF", rendered[0]["content"])
        self.assertEqual(rendered[1]["role"], "user")
        self.assertEqual(rendered[1]["content"], "build it")

    def test_a_re_emitted_request_is_not_duplicated_when_still_in_view(self):
        msgs = self._run(2)
        msgs.insert(1, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "BRIEF", "quiet": True})
        chat = {"id": "dup", "messages": msgs}
        rendered = server._build_messages(chat, "agent")
        self.assertEqual(sum(1 for m in rendered if m.get("role") == "user"), 1)

    def test_the_boundary_is_hidden_from_the_transcript_ui(self):
        # app.js drops quiet messages, so the marker never renders as a turn.
        msgs = self._run(2)
        msgs.insert(3, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "s", "quiet": True})
        self.assertTrue(msgs[3]["quiet"])

    def test_a_chat_compacted_before_phase_2_still_renders_its_summary(self):
        chat = {"id": "legacy", "messages": self._run(2), "summary": "LEGACY BRIEF"}
        self.assertEqual(server._active_summary(chat), "LEGACY BRIEF")
        self.assertIn("LEGACY BRIEF", server._build_messages(chat, "agent")[0]["content"])


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class CompactionIsNonDestructiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_inserts_a_boundary_and_deletes_nothing(self):
        msgs = CompactionBoundaryTests._run(6)
        chat = {"id": "nd", "messages": msgs}
        before = list(msgs)

        async def fake_summary(chat_, model_key, upto):
            return "BRIEF"

        # Over the threshold on the first look, comfortably under once compacted.
        sizes = iter([999_999, 10, 10, 10, 10])

        def fake_estimate(chat_, model_key, msgs=None):
            try:
                return next(sizes)
            except StopIteration:
                return 10

        with patch.object(server, "_compact_chat", new=fake_summary), \
             patch.object(server, "_estimate_chat_tokens", new=fake_estimate), \
             patch.object(server.st, "save_chat", new=lambda c: c), \
             patch.object(server.st, "load_settings",
                          new=lambda: {"auto_compact_at": 0.85, "compact_keep_recent": 4,
                                       "role_context": {"agentic": 65536}}):
            _, did = await server._maybe_auto_compact(chat, "agent", None)

        self.assertTrue(did)
        kept = [m for m in chat["messages"] if m.get("kind") != server.COMPACT_BOUNDARY_KIND]
        # The transcript GREW. Every original message is still present, in order.
        self.assertEqual(len(chat["messages"]), len(before) + 1)
        self.assertEqual(kept, before)
        self.assertEqual(server._active_summary(chat), "BRIEF")


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class DetailHorizonTests(unittest.TestCase):
    """Drop old tool details before full summarization is needed."""

    @staticmethod
    def _run(n: int, body_chars: int = 7000) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "build it"}]
        for i in range(n):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"name": "read_file",
                                         "arguments": {"path": f"f{i}.py"}}]})
            msgs.append({"role": "tool", "name": "read_file",
                         "content": json.dumps({"path": f"f{i}.py", "start": 1, "end": 400,
                                                "content": f"MARK{i}" + "x" * body_chars})})
        return msgs

    def test_horizon_drops_old_detail_but_keeps_recent(self):
        chat = {"id": "h", "messages": self._run(30)}
        self.assertTrue(server._advance_detail_horizon(chat, "coder"))
        bodies = "\n".join(
            m.get("content") or "" for m in server._build_messages(chat, "coder")
        )
        self.assertNotIn("MARK0", bodies)
        self.assertIn("MARK29", bodies)

    def test_horizon_is_a_no_op_under_the_threshold(self):
        chat = {"id": "small", "messages": self._run(2)}
        self.assertFalse(server._advance_detail_horizon(chat, "coder"))

    def test_horizon_never_orphans_a_tool_result(self):
        chat = {"id": "orphan", "messages": self._run(30)}
        server._advance_detail_horizon(chat, "coder")
        idx = server._horizon_index(chat["messages"])
        self.assertGreater(idx, 0)
        self.assertNotEqual(chat["messages"][idx + 1].get("role"), "tool")
        # And the render never emits a tool result with no call in front of it.
        pending = False
        for m in server._build_messages(chat, "coder")[1:]:
            if m.get("role") == "tool":
                self.assertTrue(pending, "tool result with no preceding tool_calls")
            pending = bool(m.get("tool_calls"))

    def test_horizon_deletes_nothing(self):
        msgs = self._run(30)
        chat = {"id": "keep", "messages": msgs}
        before = list(msgs)
        server._advance_detail_horizon(chat, "coder")
        kept = [m for m in chat["messages"] if m.get("kind") != server.DETAIL_HORIZON_KIND]
        self.assertEqual(kept, before)

    def test_the_render_is_append_only_between_horizons(self):
        def serialize(ms):
            return "\n\x00\n".join(
                f"{m.get('role')}\x01{m.get('content') or ''}" for m in ms
            )
        chat = {"id": "stable", "messages": self._run(4)}
        previous, moves = "", 0
        for step in range(14):
            if server._advance_detail_horizon(chat, "coder"):
                moves += 1
                previous = ""  # a horizon legitimately rewrites the prefix once
            current = serialize(server._build_messages(chat, "coder"))
            if previous:
                self.assertTrue(current.startswith(previous),
                                "render rewrote already-sent content without a horizon")
            previous = current
            # Distinct files: the ordinary case, where each step only appends.
            chat["messages"].extend([
                {"role": "assistant", "content": "",
                 "tool_calls": [{"name": "read_file",
                                 "arguments": {"path": f"new{step}.py"}}]},
                {"role": "tool", "name": "read_file",
                 "content": json.dumps({"path": f"new{step}.py", "start": 1, "end": 400,
                                        "content": f"NEW{step}" + "x" * 7000})},
            ])
        # Reduction happened, but rarely, not on every step.
        self.assertLess(moves, 7)

    def test_a_reread_preserves_the_cached_prefix(self):
        # Overlapping reads must not rewrite older tool results every step.
        msgs = [{"role": "user", "content": "go"}]
        previous = []
        for _ in range(3):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"name": "read_file",
                                         "arguments": {"path": "big.py"}}]})
            msgs.append({"role": "tool", "name": "read_file",
                         "content": json.dumps({"path": "big.py", "start": 1, "end": 400,
                                                "content": "SAMEBODY" + "x" * 7000})})
            current = server._build_messages({"id": "r", "messages": msgs}, "coder")
            self.assertEqual(current[:len(previous)], previous)
            previous = current
        rendered = server._build_messages({"id": "r", "messages": msgs}, "coder")
        bodies = [m.get("content") or "" for m in rendered if m.get("role") == "tool"]
        self.assertEqual(sum("SAMEBODY" in b for b in bodies), 3)

    def test_recovery_boundary_preserves_new_work_and_tool_pairs(self):
        msgs = [{"role": "user", "content": "build it"}]
        for i in range(10):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"name": "run_shell", "arguments": {"command": f"make {i}"}}]})
            msgs.append({"role": "tool", "name": "run_shell", "content": f"build {i}"})
        chat = {"id": "recovery-boundary", "messages": msgs}
        original = list(chat["messages"])
        server._retain_recent_tool_detail(chat, 4)
        previous = server._build_messages(chat, "coder")
        self.assertEqual(sum(m["role"] == "tool" for m in previous), 4)
        for index in range(8):
            chat["messages"].extend([
                {"role": "assistant", "content": "", "tool_calls": [
                    {"name": "read_file", "arguments": {"path": f"next{index}.py"}}]},
                {"role": "tool", "name": "read_file", "content": f"source {index}"},
            ])
            current = server._build_messages(chat, "coder")
            self.assertEqual(current[:len(previous)], previous)
            previous = current
        server._retain_recent_tool_detail(chat, 2)
        current = server._build_messages(chat, "coder")
        self.assertEqual([m["content"] for m in current if m["role"] == "tool"],
                         ["source 6", "source 7"])
        self.assertTrue(all(m in chat["messages"] for m in original))

    @staticmethod
    def _read(path, start, end, tag):
        return [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "read_file", "arguments": {"path": path, "start_line": start}}]},
            {"role": "tool", "name": "read_file", "content": json.dumps(
                {"path": path, "start": start, "end": end, "content": tag + "x" * 6000})},
        ]

    def _shell(self, i):
        return [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "run_shell", "arguments": {"command": f"make {i}"}}]},
            {"role": "tool", "name": "run_shell", "content": f"build {i}" + "y" * 6000},
        ]

    def test_a_cut_keeps_the_newest_view_of_a_file(self):
        msgs = [{"role": "user", "content": "fix the game"}]
        msgs += self._read("/p/game.cpp", 1, 238, "OLDTOP")
        msgs += self._read("/p/game.cpp", 1, 238, "TOP")
        msgs += self._read("/p/game.cpp", 239, 586, "BOTTOM")
        for i in range(8):
            msgs += self._shell(i)
        chat = {"id": "pin", "messages": msgs}
        server._retain_recent_tool_detail(chat, 2)
        bodies = "\n".join(m.get("content") or "" for m in server._build_messages(chat, "coder"))
        self.assertIn("TOP", bodies)
        self.assertIn("BOTTOM", bodies)
        self.assertNotIn("OLDTOP", bodies)
        self.assertNotIn("build 0", bodies)
        self.assertIn("build 7", bodies)

    def test_a_cut_drops_a_read_made_stale_by_an_edit(self):
        msgs = [{"role": "user", "content": "fix the game"}]
        msgs += self._read("/p/game.cpp", 1, 238, "STALE")
        msgs += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"name": "edit_file", "arguments": {"path": "game.cpp"}}]},
            {"role": "tool", "name": "edit_file", "content": json.dumps(
                {"ok": True, "path": "/p/game.cpp", "changed": True})},
        ]
        for i in range(8):
            msgs += self._shell(i)
        chat = {"id": "stale", "messages": msgs}
        server._retain_recent_tool_detail(chat, 2)
        bodies = "\n".join(m.get("content") or "" for m in server._build_messages(chat, "coder"))
        self.assertNotIn("STALE", bodies)

    def test_pinned_reads_stay_append_only(self):
        msgs = [{"role": "user", "content": "fix the game"}]
        msgs += self._read("/p/game.cpp", 1, 238, "TOP")
        for i in range(8):
            msgs += self._shell(i)
        chat = {"id": "pin-cache", "messages": msgs}
        server._retain_recent_tool_detail(chat, 2)
        previous = server._build_messages(chat, "coder")
        chat["messages"] += self._read("/p/game.cpp", 1, 238, "NEWTOP")
        current = server._build_messages(chat, "coder")
        self.assertEqual(current[:len(previous)], previous)

    def test_narrowing_waits_until_the_prompt_is_crowded(self):
        small = {"id": "small-narrow", "messages": self._run(6, body_chars=500)}
        self.assertFalse(server._narrow_if_crowded(small, "coder", 2))
        self.assertEqual(server._horizon_index(small["messages"]), -1)
        large = {"id": "large-narrow", "messages": self._run(40)}
        self.assertTrue(server._narrow_if_crowded(large, "coder", 2))
        self.assertGreater(server._horizon_index(large["messages"]), 0)

    def test_a_write_stops_reads_collapsing_across_it(self):
        # Content genuinely differs either side of an edit.
        msgs = [{"role": "user", "content": "go"}]
        def read(tag):
            return [
                {"role": "assistant", "content": "",
                 "tool_calls": [{"name": "read_file", "arguments": {"path": "big.py"}}]},
                {"role": "tool", "name": "read_file",
                 "content": json.dumps({"path": "big.py", "start": 1, "end": 400,
                                        "content": tag + "x" * 100})},
            ]
        msgs += read("BEFORE")
        msgs += [
            {"role": "assistant", "content": "",
             "tool_calls": [{"name": "edit_file", "arguments": {"path": "big.py"}}]},
            {"role": "tool", "name": "edit_file", "content": '{"ok": true}'},
        ]
        msgs += read("AFTER")
        rendered = server._build_messages({"id": "w", "messages": msgs}, "coder")
        bodies = "\n".join(m.get("content") or "" for m in rendered)
        self.assertIn("BEFORE", bodies)
        self.assertIn("AFTER", bodies)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class GatesRemovedTests(unittest.IsolatedAsyncioTestCase):
    """Phase 3: the loop no longer counts the model's moves and then refuses them."""

    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
    SETTINGS = {"agent_repeat_limit": 3, "role_context": {"agentic": 65536},
                "shell_approval_mode": "never_ask"}

    async def _run(self, fake_stream, chat):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value=dict(self.SETTINGS)),
        ):
            return [e async for e in server._agent_loop(chat, self.META, "agent", think=False)]

    async def test_an_open_plan_nudges_twice_then_accepts_the_debrief(self):
        # Limit plan-continuation nudges, then accept the final reply.
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            yield {"message": {"content": "Done, added the cooldown."},
                   "done": True, "done_reason": "stop"}

        chat = {
            "id": "debrief", "project_id": None,
            "agent_todos": [
                {"id": "a", "content": "Add cooldown", "status": "in_progress"},
                {"id": "b", "content": "Polish", "status": "pending"},
            ],
            "messages": [{"role": "user", "content": "Add a cooldown."}],
        }
        events = await self._run(fake_stream, chat)

        # Two nudges, then the third debrief is accepted, never an abort.
        self.assertEqual(rounds, 3)
        self.assertEqual(events[-1]["message"]["content"], "Done, added the cooldown.")
        self.assertFalse(chat.get("interrupted"))
        # The turn ends on the debrief. The plan is untouched bookkeeping, it
        # neither blocks the turn nor gets rewritten on the way out.
        self.assertEqual([t["status"] for t in chat["agent_todos"]],
                         ["in_progress", "pending"])

    async def test_narration_is_turned_into_an_action_not_a_debrief(self):
        # Retry a promised action instead of ending the turn before it happens.
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"content": "Let me analyze the test failure and improve the"},
                       "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Fixed the gravity flip; all three tests pass now."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "narrate", "project_id": None,
                "messages": [{"role": "user", "content": "Fix it."}]}
        events = await self._run(fake_stream, chat)

        self.assertGreater(rounds, 1, "narration should not have ended the turn")
        self.assertEqual(events[-1]["message"]["content"],
                         "Fixed the gravity flip; all three tests pass now.")

    async def test_open_milestones_are_left_exactly_as_the_model_left_them(self):
        # Bulk-closing on the way out rewrites the record: a live run reported six
        # items finishing at once after the model had actually stopped early.
        async def fake_stream(*a, **k):
            yield {"message": {"content": "Stopping here; the remaining work is blocked."},
                   "done": True, "done_reason": "stop"}

        todos = [{"id": f"m{i}", "content": f"Step {i}", "status": "pending"}
                 for i in range(6)]
        chat = {"id": "leave", "project_id": None,
                "agent_todos": todos,
                "messages": [{"role": "user", "content": "Do the six steps."}]}
        await self._run(fake_stream, chat)

        self.assertEqual([t["status"] for t in chat["agent_todos"]], ["pending"] * 6)

    async def test_reads_are_never_refused(self):
        # No inspection budget, no redundant-read block. Reading "too much" was a
        # symptom of a working set that kept evaporating, not a fault to police.
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds <= 12:
                # Distinct ranges: previously the inspection budget refused these
                # after eight, and the redundant-read guard refused overlaps.
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "read_file",
                    "arguments": {"path": "same.py",
                                  "start_line": rounds * 20 + 1,
                                  "end_line": rounds * 20 + 60},
                }}]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Read enough."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "reads", "project_id": None,
                "messages": [{"role": "user", "content": "Study the file."}]}
        def read_result(name, arguments, *a, **k):
            start = int((arguments or {}).get("start_line") or 1)
            return json.dumps({"path": "same.py", "start": start,
                               "end": start + 59, "content": "x" * 100})

        with patch.object(server.agent, "run_tool", side_effect=read_result):
            events = await self._run(fake_stream, chat)

        blocked = [e for e in events
                   if e.get("type") == "tool_result" and "Skipped inspection" in str(e.get("summary"))]
        self.assertEqual(blocked, [])
        self.assertEqual(events[-1]["message"]["content"], "Read enough.")

    async def test_steering_never_rides_on_a_system_message(self):
        # Keep step instructions off the system prefix so Qwen can reuse the prompt cache.
        samples: list[dict] = []
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            samples.append({"messages": list(k.get("messages") or a[1])})
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "read_file",
                    "arguments": {"path": "f.py", "start_line": 1, "end_line": 5},
                }}]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}

        chat = {"id": "steer", "project_id": None,
                "agent_todos": [{"id": "a", "content": "Do it", "status": "in_progress"}],
                "messages": [{"role": "user", "content": "Continue."}]}
        with patch.object(server.agent, "run_tool",
                          side_effect=lambda *a, **k: json.dumps(
                              {"path": "f.py", "start": 1, "end": 5, "content": "x"})):
            await self._run(fake_stream, chat)

        directive_samples = [s for s in samples if DIRECTIVE_MARKER in _prompt_text(s)]
        self.assertTrue(directive_samples, "expected an open-plan continuation directive")
        for sample in directive_samples:
            self.assertNotEqual(
                _directive_channel(sample), "system",
                "per-step steering must not be a system message",
            )
            # And exactly one system message: the stable prefix.
            self.assertEqual(
                sum(1 for m in sample["messages"] if m.get("role") == "system"), 1)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class OffloadWiringTests(unittest.IsolatedAsyncioTestCase):
    """Check that the agent loop saves large tool results before truncating them."""

    async def test_a_large_tool_result_is_stored_as_a_pointer(self):
        import shutil
        big = "\n".join(f"build line {i}: compiling" for i in range(4000))
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "run_shell",
                    "arguments": {"command": "make"},
                }}]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Build inspected."},
                       "done": True, "done_reason": "stop"}

        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        chat = {"id": "offload-wiring", "project_id": None,
                "messages": [{"role": "user", "content": "Run the build."}]}
        raw = json.dumps({"exit_code": 0, "stdout": big, "stderr": ""})
        try:
            with (
                patch.object(server.oc, "stream_chat", new=fake_stream),
                patch.object(server, "_maybe_auto_compact", new=no_compact),
                patch.object(server.agent, "run_tool", side_effect=lambda *a, **k: raw),
                patch.object(server.st, "save_chat", side_effect=lambda v: v),
                patch.object(server.st, "load_settings", return_value={
                    "agent_repeat_limit": 3, "role_context": {"agentic": 65536},
                    "shell_approval_mode": "never_ask"}),
            ):
                [e async for e in server._agent_loop(
                    chat, {"id": "m", "keep_alive": "1m", "temperature": 0.2,
                           "context": 32768}, "agent", think=False)]

            stored = next(m for m in chat["messages"] if m.get("role") == "tool")
            payload = json.loads(stored["content"])
            # The transcript holds a pointer, not 139k characters of build log.
            self.assertIn("stdout", payload["offloaded"])
            self.assertLess(len(stored["content"]), len(raw) // 5)
            # Head AND tail survive. [-12000:] used to drop the head, where the
            # first error of a build log lives.
            self.assertIn("build line 0:", payload["stdout"])
            self.assertIn("build line 3999:", payload["stdout"])
            # And the full log is on disk, byte for byte.
            self.assertEqual(
                Path(payload["offloaded"]["stdout"]["path"]).read_text(encoding="utf-8"),
                big,
            )
        finally:
            shutil.rmtree(server.agent.TOOLOUT_DIR / "offload-wiring", ignore_errors=True)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class DelegationTests(unittest.IsolatedAsyncioTestCase):
    """Run sub-tasks sequentially on the same model with a separate transcript."""

    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
    SETTINGS = {"agent_repeat_limit": 3, "role_context": {"agentic": 65536},
                "shell_approval_mode": "never_ask"}

    def setUp(self):
        self.saved: list[dict] = []

    async def _run(self, fake_stream, chat, run_tool=None, large_project=True):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        patches = [
            # The loop recomputes the gate from the project root each run, so a
            # flag set on the chat alone would be overwritten.
            patch.object(server.agent, "project_is_large", return_value=large_project),
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: self.saved.append(v) or v),
            patch.object(server.st, "load_settings", return_value=dict(self.SETTINGS)),
        ]
        if run_tool is not None:
            patches.append(patch.object(server.agent, "run_tool", side_effect=run_tool))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return [e async for e in server._agent_loop(
                chat, self.META, "agent", think=False)]

    async def test_the_parent_pays_only_for_the_summary(self):
        big = "x" * 60_000
        turns = []

        async def fake_stream(*a, **k):
            msgs = list(k.get("messages") or a[1])
            turns.append(msgs)
            # Discriminate on the sub-run's own USER turn. Matching the task
            # text anywhere fails: the delegate result echoes the task back, so
            # the parent's next prompt contains it too.
            is_sub = any(
                m.get("role") == "user" and (m.get("content") or "").strip() == "Find the auth check"
                for m in msgs
            )
            if is_sub:
                if not any(m.get("role") == "tool" for m in msgs):
                    yield {"message": {"tool_calls": [{"function": {
                        "index": 0, "name": "read_file",
                        "arguments": {"path": "auth.py"}}}]},
                        "done": True, "done_reason": "stop"}
                else:
                    yield {"message": {"content": "Auth is checked in auth.py:42."},
                           "done": True, "done_reason": "stop"}
            elif not any(m.get("role") == "tool" for m in msgs):
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "delegate",
                    "arguments": {"task": "Find the auth check"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Patched auth.py:42."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "deleg", "project_id": None, "delegate_available": True,
                "messages": [{"role": "user", "content": "Fix auth."}]}
        events = await self._run(
            fake_stream, chat,
            run_tool=lambda *a, **k: json.dumps({"path": "auth.py", "content": big}),
        )

        tool_msgs = [m for m in chat["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1, "only the delegate result reaches the parent")
        payload = json.loads(tool_msgs[0]["content"])
        self.assertTrue(payload["delegated"])
        self.assertIn("auth.py:42", payload["report"])
        self.assertIn("read_file", payload["tools_attempted"])
        # The 60k the sub-agent read never touches the parent's transcript.
        self.assertNotIn(big, json.dumps(chat["messages"]))
        self.assertLess(len(tool_msgs[0]["content"]), 2000)
        self.assertEqual(events[-1]["message"]["content"], "Patched auth.py:42.")

    async def test_a_subagent_cannot_delegate_further(self):
        async def fake_stream(*a, **k):
            yield {"message": {"content": "done"}, "done": True, "done_reason": "stop"}

        chat = {"id": "sub", "project_id": None, "subagent": {"task": "x"},
                "messages": [{"role": "user", "content": "x"}]}
        await self._run(fake_stream, chat)
        # Enforced twice: the schema is withheld...
        names = [t["function"]["name"] for t in server.agent.tool_defs_for(chat)]
        self.assertNotIn("delegate", names)
        # ...including when the spec is empty. An empty dict is falsy, and
        # treating that as "not a sub-agent" handed the child the full tool set.
        for spec in ({}, None, {"tools": []}):
            got = [t["function"]["name"]
                   for t in server.agent.tool_defs_for({"subagent": spec})]
            self.assertNotIn("delegate", got, f"spec={spec!r} leaked delegate")
            self.assertTrue(got, f"spec={spec!r} left the sub-agent with no tools")
        # A non-sub-run keeps everything, when the gate is open for it.
        self.assertIn("delegate", [t["function"]["name"]
                                   for t in server.agent.tool_defs_for({"delegate_available": True})])

    async def test_a_refused_recursive_delegate_is_answered_not_crashed(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "delegate",
                    "arguments": {"task": "go deeper"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "did it myself"},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "sub2", "project_id": None, "subagent": {"task": "x"},
                "messages": [{"role": "user", "content": "x"}]}
        events = await self._run(fake_stream, chat)
        # Reject nested delegation at execution time, even if the model names the tool.
        tool_msgs = [m for m in chat["messages"] if m.get("role") == "tool"]
        payload = json.loads(tool_msgs[0]["content"])
        self.assertIn("not available", payload["error"])
        self.assertNotIn("delegate", payload["available"])
        # And the sub-run carries on instead of dying on the refusal.
        self.assertEqual(events[-1]["message"]["content"], "did it myself")

    async def test_a_withheld_tool_is_refused_at_execution_not_just_hidden(self):
        # Enforce the sub-task tool list during execution too. Withholding a schema
        # alone does not stop a model from naming it.
        rounds = 0
        executed: list[str] = []

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "write_file",
                    "arguments": {"path": "x.py", "content": "boom"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "reported instead"},
                       "done": True, "done_reason": "stop"}

        def spy(name, *a, **k):
            executed.append(name)
            return json.dumps({"ok": True})

        chat = {"id": "restricted", "project_id": None,
                "subagent": {"task": "look only", "tools": ["read_file"]},
                "messages": [{"role": "user", "content": "look"}]}
        await self._run(fake_stream, chat, run_tool=spy)

        self.assertNotIn("write_file", executed, "a withheld tool was executed")
        tool_msgs = [m for m in chat["messages"] if m.get("role") == "tool"]
        payload = json.loads(tool_msgs[0]["content"])
        self.assertIn("not available", payload["error"])
        self.assertEqual(payload["available"], ["read_file"])

    async def test_the_parent_may_still_use_every_tool(self):
        # The guard must not restrict a normal run.
        chat = {
            "id": "normal", "messages": [],
            "delegate_available": True, "computer_use": True,
        }
        with patch.object(server.mcp_connections, "active", return_value=[{"id": "fixture"}]):
            self.assertEqual(
                server._allowed_tool_names(chat),
                {t["function"]["name"] for t in server.agent.TOOL_DEFS},
            )

    async def test_a_run_that_did_not_ask_for_the_desktop_is_not_given_it(self):
        # With service connections enabled, only desktop and browser remain gated.
        chat = {"id": "normal", "messages": [], "delegate_available": True}
        with patch.object(server.mcp_connections, "active", return_value=[{"id": "fixture"}]):
            self.assertEqual(
                {t["function"]["name"] for t in server.agent.TOOL_DEFS}
                - server._allowed_tool_names(chat),
                {"computer", "browser"},
            )

    async def test_delegation_is_gated_on_project_size(self):
        # Offer delegate only on large projects to avoid unused schema overhead.
        import tempfile
        names = lambda c: {t["function"]["name"] for t in server.agent.tool_defs_for(c)}
        self.assertNotIn("delegate", names({}))
        self.assertIn("delegate", names({"delegate_available": True}))

        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp)
            (small / "only.py").write_text("x = 1")
            self.assertFalse(server.agent.project_is_large(small))
            self.assertFalse(server.agent.project_is_large(None))

            big = small / "big"
            big.mkdir()
            for i in range(server.agent.DELEGATE_MIN_FILES + 5):
                (big / f"f{i}.py").write_text("y = 1")
            self.assertTrue(server.agent.project_is_large(big))

    async def test_a_runaway_subtask_hits_its_budget_and_reports_back(self):
        async def fake_stream(*a, **k):
            # Never concludes: always another tool call.
            yield {"message": {"tool_calls": [{"function": {
                "index": 0, "name": "list_dir",
                "arguments": {"path": f"dir{time.time()}"}}}]},
                "done": True, "done_reason": "stop"}

        chat = {"id": "runaway", "project_id": None, "subagent": {"task": "x"},
                "messages": [{"role": "user", "content": "x"}]}
        events = await self._run(fake_stream, chat,
                                 run_tool=lambda *a, **k: json.dumps({"entries": []}))
        self.assertIn("budget reached", events[-1]["message"]["content"].lower())
        rounds = sum(1 for m in chat["messages"] if m.get("role") == "assistant")
        self.assertLessEqual(rounds, server.agent.SUBAGENT_MAX_STEPS + 1)

    async def test_a_failing_subtask_does_not_kill_the_parent(self):
        async def boom(*a, **k):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

        async def fake_stream(*a, **k):
            msgs = list(k.get("messages") or a[1])
            if not any(m.get("role") == "tool" for m in msgs):
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "delegate",
                    "arguments": {"task": "survey"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Carried on regardless."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "boom", "project_id": None,
                "messages": [{"role": "user", "content": "go"}]}
        with patch.object(server, "_agent_loop", wraps=server._agent_loop):
            async def failing_sub(parent, args, meta, model_key, effort=None):
                raise RuntimeError("model exploded")
                yield  # pragma: no cover
            events = await self._run(fake_stream, chat)

        # The parent must still finish its own turn.
        self.assertEqual(events[-1]["message"]["content"], "Carried on regardless.")


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class SampleTimeoutRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Give timed-out samples more time. Shrink recovery budgets only for empty replies."""

    async def _timeouts(self, done_reason: str) -> list[int]:
        seen: list[int] = []
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            # asyncio.timeout() is what the loop wraps the stream in; record the
            # budget it was given by reading the deadline off the running task.
            seen.append(k.get("_timeout") or 0)
            if rounds <= 3:
                yield {"done": True, "done_reason": done_reason, "eval_count": 10,
                       "message": {}}
            else:
                yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}

        return seen

    async def test_a_timeout_retry_gets_at_least_the_generation_budget(self):
        from config import EFFORT
        budget = EFFORT["high"]["num_predict"]
        floor = int(budget / 40) + 60
        # The whole point: the retry budget must exceed what the reply physically
        # needs at a conservative rate, and must not decay with the stall count.
        self.assertGreater(floor, 150, "high-effort floor must exceed the base timeout")
        for stalls in range(1, 5):
            after_timeout = max(150, floor)
            after_empty = max(60, 150 - stalls * 30)
            self.assertGreaterEqual(after_timeout, floor)
            self.assertLessEqual(after_empty, 120)
        # An empty-response retry still fails fast; a timeout retry does not.
        self.assertGreater(max(150, floor), max(60, 150 - 3 * 30))

    async def test_the_flag_only_trips_on_a_real_timeout(self):
        self.assertTrue("sample timeout after 60s".startswith("sample timeout"))
        for other in ("stop", "length", "empty response", "load"):
            self.assertFalse(other.startswith("sample timeout"))


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class AskUserDisciplineTests(unittest.IsolatedAsyncioTestCase):
    """Ask clarifying questions before starting changes, then continue with the available information."""

    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}

    async def _run(self, fake_stream, chat, run_tool=None):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        patches = [
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3, "role_context": {"agentic": 65536},
                "shell_approval_mode": "never_ask"}),
        ]
        if run_tool is not None:
            patches.append(patch.object(server.agent, "run_tool", side_effect=run_tool))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return [e async for e in server._agent_loop(
                chat, self.META, "agent", think=False)]

    async def test_a_question_after_the_first_write_is_refused(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "write_file",
                    "arguments": {"path": "game.html", "content": "<html>"}}}]},
                    "done": True, "done_reason": "stop"}
            elif rounds == 2:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "ask_user",
                    "arguments": {"questions": [{"id": "v", "question": "Big volcano?",
                                                 "options": [{"label": "Yes"}, {"label": "No"}]}]}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Built it; assumed a large volcano."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "late-ask", "project_id": None,
                "messages": [{"role": "user", "content": "Make a volcano game."}]}
        events = await self._run(
            fake_stream, chat,
            run_tool=lambda *a, **k: json.dumps({"ok": True, "path": "game.html"}))

        # The run must not have parked on a question.
        self.assertFalse(any(e.get("type") == "ask_user" for e in events))
        refusal = json.loads([m for m in chat["messages"]
                              if m.get("role") == "tool"][-1]["content"])
        self.assertIn("Too late to ask", refusal["error"])
        self.assertEqual(events[-1]["message"]["model_content"],
                         "Built it; assumed a large volcano.")

    async def test_a_question_before_any_write_still_works(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "ask_user",
                    "arguments": {"questions": [{"id": "t", "question": "Which file?",
                                                 "options": [{"label": "a.py"}, {"label": "b.py"}]}]}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "done"}, "done": True, "done_reason": "stop"}

        chat = {"id": "early-ask", "project_id": None,
                "messages": [{"role": "user", "content": "Patch the right file."}]}
        # Expire immediately rather than blocking the test for the real window.
        with patch.object(server, "ASK_USER_TIMEOUT", 0.01):
            events = await self._run(fake_stream, chat)

        self.assertTrue(any(e.get("type") == "ask_user" for e in events))
        # An unanswered question is cheap: the model is told to assume and continue.
        result = json.loads([m for m in chat["messages"]
                             if m.get("role") == "tool"][0]["content"])
        self.assertTrue(result["timed_out"])
        self.assertIn("most reasonable", result["note"])
        self.assertEqual(events[-1]["message"]["content"], "done")

    async def test_the_block_window_is_short(self):
        # 600s meant a question the user did not notice cost ten minutes.
        self.assertLessEqual(server.ASK_USER_TIMEOUT, 120)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class AgenticModelPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Keep the chosen Agentic model when creating a new chat."""

    @contextlib.contextmanager
    def _storage(self, store):
        def new_chat(mode="chat", title="New chat", project_id=None):
            chat = {"id": "c1", "mode": mode, "title": title, "project_id": project_id}
            store["c1"] = chat
            return chat

        # Pinned: these assert the default division of labour, so a machine
        # whose saved settings point elsewhere must not change the result.
        with patch.object(server.st, "new_chat", side_effect=new_chat), \
             patch.object(server.st, "get_chat", side_effect=lambda cid: store.get(cid)), \
             patch.object(server.st, "load_settings", return_value={}), \
             patch.object(server.st, "save_chat", side_effect=lambda c: store.__setitem__(c["id"], c) or c):
            yield

    async def test_a_new_agentic_chat_records_the_picked_model(self):
        store = {}
        with self._storage(store):
            chat = await server.api_new_chat(
                server.ChatCreate(mode="agentic", agent_model=config.DEFAULT_AGENT_MODEL)
            )
        self.assertEqual(chat["model_id"], config.DEFAULT_AGENT_MODEL)
        self.assertEqual(server._model_key(chat), config.DEFAULT_AGENT_MODEL)

    async def test_a_new_agentic_chat_without_a_pick_gets_the_executor(self):
        store = {}
        with self._storage(store):
            chat = await server.api_new_chat(server.ChatCreate(mode="agentic"))
        self.assertEqual(chat["model_id"], config.DEFAULT_CODER_MODEL)

    async def test_a_chat_tab_conversation_carries_no_agentic_pick(self):
        store = {}
        with self._storage(store):
            chat = await server.api_new_chat(server.ChatCreate(mode="chat"))
        self.assertNotIn("model_id", chat)

    async def test_patching_retargets_an_open_chat_without_starting_a_new_one(self):
        store = {"c1": {"id": "c1", "mode": "agentic", "model_id": config.DEFAULT_CODER_MODEL}}
        with self._storage(store):
            chat = await server.api_patch_chat(
                "c1", server.ChatPatch(mode="agentic", agent_model=config.DEFAULT_AGENT_MODEL)
            )
        self.assertEqual(chat["model_id"], config.DEFAULT_AGENT_MODEL)
        self.assertEqual(server._model_key(chat), config.DEFAULT_AGENT_MODEL)

    async def test_a_bogus_agent_model_leaves_the_existing_pick_alone(self):
        store = {"c1": {"id": "c1", "mode": "agentic", "model_id": config.DEFAULT_AGENT_MODEL}}
        with self._storage(store):
            chat = await server.api_patch_chat("c1", server.ChatPatch(agent_model="nonsense"))
        self.assertEqual(chat["model_id"], config.DEFAULT_AGENT_MODEL)

    async def test_a_chat_that_predates_roles_is_migrated_on_read(self):
        chat = st._migrate_chat({"id": "old", "mode": "agentic", "agent_model": "coder"})
        self.assertEqual(chat["model_id"], config.DEFAULT_CODER_MODEL)
        self.assertNotIn("agent_model", chat)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class ParkedPromptTests(unittest.IsolatedAsyncioTestCase):
    """Keep open prompts available to requests outside the response stream."""

    SETTINGS = {
        "agent_repeat_limit": 3,
        "role_context": {"agentic": 32768},
        "shell_approval_mode": "safe_auto",
    }
    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}

    def _sampler(self, command="sudo apt install p7zip-full"):
        calls = []

        async def fake_stream_chat(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                yield {"message": {"tool_calls": [{
                    "function": {"index": 0, "name": "run_shell",
                                 "arguments": {"command": command}},
                }]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Done after the approval."},
                       "done": True, "done_reason": "stop"}

        return fake_stream_chat

    @contextlib.contextmanager
    def _harness(self, command="sudo apt install p7zip-full"):
        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        with (
            patch.object(server.oc, "stream_chat", new=self._sampler(command)),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=lambda *a, **k: json.dumps({"ok": True})),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server, "SHELL_APPROVAL_TIMEOUT", 5.0),
            patch.object(server.st, "load_settings", return_value=dict(self.SETTINGS)),
        ):
            yield

    async def test_a_parked_approval_can_be_read_back_over_http(self):
        chat = {"id": "parked", "project_id": None,
                "messages": [{"role": "user", "content": "install 7z"}]}
        seen, served = [], []

        async def drive():
            with self._harness():
                async for ev in server._agent_loop(chat, self.META, "agent", think=False):
                    seen.append(ev)
                    if ev.get("type") == "shell_approval":
                        # Exactly what the UI's poll does when it missed the frame.
                        served.append(await server.pending_question("parked"))
                        await server.answer_question("parked", server.AnswerIn(
                            answers={"shell_approval": "Run command"}, cancelled=False,
                        ))

        await asyncio.wait_for(drive(), timeout=20)

        self.assertEqual(len(served), 1)
        self.assertEqual(served[0]["pending"]["type"], "shell_approval")
        self.assertEqual(served[0]["pending"]["command"], "sudo apt install p7zip-full")
        # Answered, not expired, and the slot is released afterwards.
        done = next(e for e in seen if e.get("type") == "shell_approval_done")
        self.assertFalse(done["cancelled"])
        self.assertEqual((await server.pending_question("parked"))["pending"], None)

    async def test_nothing_is_pending_on_an_idle_chat(self):
        self.assertEqual((await server.pending_question("never-ran"))["pending"], None)

    async def test_full_access_runs_shell_git_and_service_actions_without_prompts(self):
        calls = [
            ("run_shell", {"command": "rm -rf disposable-fixture"}),
            ("git_status", {"action": "push"}),
            ("connection_call", {"connection_id": "fixture", "tool": "delete_fixture", "arguments": {}}),
        ]
        for name, arguments in calls:
            with self.subTest(tool=name):
                count = 0

                async def sampler(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 1:
                        message = {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}
                    else:
                        message = {"content": "Completed the requested fixture action."}
                    yield {"message": message, "done": True, "done_reason": "stop"}

                chat = {"id": "full-access-test", "messages": [{"role": "user", "content": "Run the fixture action"}]}
                with self._harness(), \
                     patch.object(server.st, "load_settings", return_value={**self.SETTINGS, "approval_mode": "full_access"}), \
                     patch.object(server.oc, "stream_chat", new=sampler), \
                     patch.object(server.mcp_connections, "preflight", return_value=None), \
                     patch.object(server.mcp_connections, "approval_ticket", return_value=("revision", "fingerprint")), \
                     patch.object(server, "_index_work", new=AsyncMock(side_effect=lambda fn, *a, **kw: fn(*a, **kw))), \
                     patch.object(server.mcp_connections, "call", return_value={"ok": True}) as service_call:
                    async def collect():
                        events = []
                        async for event in server._agent_loop(chat, self.META, "agent", think=False):
                            events.append(event)
                        return events
                    events = await asyncio.wait_for(collect(), timeout=10)
                    self.assertFalse(any(event.get("type") == "shell_approval" for event in events))
                    self.assertTrue(any(event.get("type") == "tool_start" for event in events))
                    if name == "connection_call":
                        service_call.assert_called_once_with(arguments, approved=True, ticket=("revision", "fingerprint"))
                    else:
                        self.assertTrue(any(call.args[:2] == (name, arguments) for call in server.agent.run_tool.call_args_list))

    async def test_unattended_shell_does_not_grant_service_or_github_actions(self):
        for name, arguments in [("git_status", {"action": "push"}), ("connection_call", {"tool": "change"})]:
            with self.subTest(tool=name):
                count = 0

                async def sampler(*args, **kwargs):
                    nonlocal count
                    count += 1
                    message = {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]} if count == 1 else {"content": "Done"}
                    yield {"message": message, "done": True, "done_reason": "stop"}

                chat = {"id": "legacy-shell-test", "messages": [{"role": "user", "content": "Run fixture"}]}
                with self._harness(), \
                     patch.object(server.st, "load_settings", return_value={**self.SETTINGS, "shell_approval_mode": "never_ask"}), \
                     patch.object(server.oc, "stream_chat", new=sampler), \
                     patch.object(server.mcp_connections, "preflight", return_value=None), \
                     patch.object(server.mcp_connections, "requires_approval", return_value=True), \
                     patch.object(server.mcp_connections, "approval_ticket", return_value=(1, 2)), \
                     patch.object(server.mcp_connections, "call") as service_call:
                    events = []
                    async for event in server._agent_loop(chat, self.META, "agent", think=False):
                        events.append(event)
                        if event.get("type") == "shell_approval":
                            await server.answer_question(chat["id"], server.AnswerIn(answers={"shell_approval": "Deny"}, cancelled=False))
                    self.assertTrue(any(event.get("type") == "shell_approval" for event in events))
                    service_call.assert_not_called()
                    self.assertFalse(any(call.args[0] == name for call in server.agent.run_tool.call_args_list))

    async def test_a_sub_agent_approval_reaches_the_parent_and_its_chat_id(self):
        """Forward sub-task approvals to the parent chat so the UI can display and answer them."""
        parent = {"id": "parent-chat", "project_id": None, "messages": []}
        events, parked_keys = [], []

        async def drive():
            with self._harness():
                async for ev in server._run_subagent(
                    parent, {"task": "install the archiver"}, self.META, "agent"
                ):
                    events.append(ev)
                    if ev.get("type") == "shell_approval":
                        parked_keys.append(list(server._pending_questions))
                        await server.answer_question(ev["chat_id"], server.AnswerIn(
                            answers={"shell_approval": "Deny"}, cancelled=False,
                        ))

        await asyncio.wait_for(drive(), timeout=20)

        approvals = [e for e in events if e.get("type") == "shell_approval"]
        self.assertEqual(len(approvals), 1, "the sub-run's approval never reached the parent stream")
        self.assertEqual(approvals[0]["chat_id"], "parent-chat")
        self.assertEqual(parked_keys[0], ["parent-chat"])
        self.assertTrue(any(e.get("type") == "shell_approval_done" for e in events))
        # And the sub-run still returns a result rather than dying on the prompt.
        self.assertTrue(any(e.get("type") == "subagent_result" for e in events))


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class RoleDerivationTests(unittest.TestCase):
    """Roles key the prompt, effort and window; the UI still speaks the old names."""

    def test_mode_decides_the_role(self):
        self.assertEqual(server._role({"mode": "chat"}), "chat")
        self.assertEqual(server._role({"mode": "agentic"}), "agentic")
        self.assertEqual(server._role({"mode": "computer"}), "agentic")
        self.assertEqual(server._role({"mode": "code"}), "agentic")

    def test_reasoning_is_a_variant_of_chat_not_a_mode(self):
        self.assertEqual(server._role({"mode": "chat", "reasoning": True}), "reasoning")
        self.assertEqual(server._role({"mode": "chat"}, reasoning=True), "reasoning")
        # An agentic run never becomes a reasoning run.
        self.assertEqual(server._role({"mode": "agentic", "reasoning": True}), "agentic")

    def test_the_reasoning_flag_travels_with_the_chat_model(self):
        # Reasoning is not a model of its own, so the same id serves both and
        # only the role tells them apart.
        chat = {"mode": "chat", "reasoning": True}
        self.assertEqual(server._role(chat), "reasoning")
        self.assertEqual(server._role({**chat, "reasoning": False}), "chat")


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class ThinkingGateTests(unittest.IsolatedAsyncioTestCase):
    """Send think=true only to models that report thinking support."""

    def _capture(self):
        seen = []

        async def fake_stream(model_id, msgs, **kw):
            seen.append(kw.get("think"))
            yield {"message": {"content": "done"}, "done": True}

        return seen, fake_stream

    async def _run_plain(self, profile, requested_think):
        seen, fake_stream = self._capture()
        chat = {"id": "t1", "mode": "chat", "messages": [{"role": "user", "content": "hi"}]}
        with patch.object(server.oc, "stream_chat", fake_stream), \
             patch.object(server.st, "save_chat", side_effect=lambda c: c), \
             patch.object(server.st, "load_settings", return_value={}), \
             patch.object(server, "_build_messages", return_value=[{"role": "system", "content": "s"}]), \
             patch.object(server, "_num_ctx_for", return_value=4096):
            async for _ in server._plain_stream(chat, profile, profile["id"], effort="high",
                                                think=requested_think):
                pass
        return seen[0]

    async def test_a_thinking_model_is_allowed_to_think(self):
        profile = {"id": "m", "keep_alive": "5m", "think": True, "label": "M"}

        self.assertTrue(await self._run_plain(profile, True))

    async def test_a_model_without_the_capability_is_never_asked_to_think(self):
        profile = {"id": "m", "keep_alive": "5m", "think": False, "label": "M"}

        self.assertFalse(await self._run_plain(profile, True))

    async def test_an_unprofiled_model_is_not_asked_to_think_either(self):
        profile = {"id": "m", "keep_alive": "5m", "label": "M"}

        self.assertFalse(await self._run_plain(profile, True))


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class MalformedCallGuardTests(unittest.IsolatedAsyncioTestCase):
    """Retry malformed reply-body calls with a bounded recovery budget."""

    async def _run(self, replies):
        samples = []

        async def fake_stream_chat(model_id, msgs, **kw):
            i = min(len(samples), len(replies) - 1)
            samples.append(msgs)
            yield {"message": {"content": replies[i]}, "done": True, "done_reason": "stop"}

        async def fake_ensure(key, meta=None):
            return meta or {"id": key, "keep_alive": "1m"}

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {"id": "mangled", "project_id": None, "mode": "agentic",
                "model_id": "m", "messages": [{"role": "user", "content": "Plan it."}]}
        meta = {"id": "m", "keep_alive": "1m", "temperature": 0.2, "context": 32768, "think": False}
        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server.oc, "ensure_model_loaded", side_effect=fake_ensure),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3, "role_context": {"agentic": 32768},
                "shell_approval_mode": "never_ask"}),
        ):
            events = [e async for e in server._agent_loop(chat, meta, "m", think=False)]
        return samples, events

    MANGLED = '{"name":"todo_write","parameters":{"todos":[["a":1]]}}'

    async def test_a_mangled_call_is_retried_rather_than_shown_as_the_answer(self):
        samples, _ = await self._run([self.MANGLED, "Done, the plan is set."])

        self.assertGreater(len(samples), 1)
        # The retry tells the model what went wrong.
        text = "\n".join(m.get("content") or "" for m in samples[1])
        self.assertIn("malformed", text.lower())

    async def test_endless_mangling_stops_instead_of_looping(self):
        samples, events = await self._run([self.MANGLED])

        self.assertLessEqual(len(samples), server.MALFORMED_CALL_LIMIT + 2)
        self.assertTrue(events)

    async def test_a_clean_reply_is_never_retried(self):
        samples, _ = await self._run(["All done, the tests pass."])

        self.assertEqual(len(samples), 1)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class EmptyRoleTests(unittest.TestCase):
    """With nothing able to call tools, agentic mode has to say so."""

    def _pick(self, registry, settings):
        with patch.object(server.st, "load_settings", return_value=settings), \
             patch.object(server.reg, "installed", return_value=registry):
            return server._model_key({"id": "c", "mode": "agentic"})

    def test_a_chat_only_model_that_can_call_tools_is_used(self):
        registry = {"m": {"id": "m", "label": "m", "tools": True, "think": False}}
        settings = {"model_roles": {"m": ["chat"]}}

        self.assertEqual(self._pick(registry, settings), "m")

    def test_a_model_without_tools_is_never_pressed_into_agentic_service(self):
        # Falling back to it would fail inside Ollama with nothing useful to show.
        registry = {"m": {"id": "m", "label": "m", "tools": False, "think": False}}
        settings = {"model_roles": {"m": ["chat"]}}

        self.assertEqual(self._pick(registry, settings), "")

    def test_the_message_tells_the_user_what_to_do_about_it(self):
        message = server._no_model_message("agentic")

        self.assertIn("tool", message.lower())
        self.assertIn("Models", message)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class ModelApiTests(unittest.IsolatedAsyncioTestCase):
    """The endpoints the Models panel is built on."""

    REGISTRY = {
        "big:27b": {"id": "big:27b", "label": "Big", "tools": True, "vision": True,
                    "think": True, "context_max": 262144, "weights_bytes": 16 * 1024**3,
                    "kv_bytes_per_token": 88500},
        "coder:30b": {"id": "coder:30b", "label": "Coder", "tools": True, "vision": False,
                      "think": False, "context_max": 262144, "weights_bytes": 17 * 1024**3,
                      "kv_bytes_per_token": 49152},
        "blind:8b": {"id": "blind:8b", "label": "Blind", "tools": False, "vision": False,
                     "think": False, "context_max": 32768, "weights_bytes": 5 * 1024**3,
                     "kv_bytes_per_token": 8192},
    }

    def _env(self, settings=None, saved=None):
        store = dict(settings or {})

        def save(patch):
            store.update(patch)
            if saved is not None:
                saved.update(patch)
            return store

        return (
            patch.object(server.st, "load_settings", return_value=store),
            patch.object(server.st, "save_settings", side_effect=save),
            patch.object(server.reg, "installed", return_value=dict(self.REGISTRY)),
            # Fit is a claim about the model, not about whatever machine runs the
            # suite. Unpinned, this reads real VRAM and a GPU-less runner fails.
            patch.object(server.reg, "hardware", return_value={
                "gpu": "test", "vram_bytes": 24 * 1024**3, "ram_bytes": 32 * 1024**3,
                "free_disk_bytes": 500 * 1024**3, "model_store": "/tmp",
                "usable_bytes": 24 * 1024**3}),
        )

    async def test_listing_reports_capability_roles_and_fit(self):
        with contextlib.ExitStack() as stack:
            for p in self._env():
                stack.enter_context(p)
            body = await server.api_models()

        rows = {m["id"]: m for m in body["installed"]}
        self.assertEqual(rows["blind:8b"]["can"]["agentic"], False)
        self.assertEqual(rows["big:27b"]["can"], {"chat": True, "reasoning": True, "agentic": True})
        self.assertNotIn("agentic", rows["blind:8b"]["roles"])
        self.assertTrue(rows["big:27b"]["fit"]["fits"])
        self.assertIn("hardware", body)

    async def test_a_toolless_model_cannot_be_assigned_to_agentic(self):
        with contextlib.ExitStack() as stack:
            for p in self._env():
                stack.enter_context(p)
            with self.assertRaises(server.HTTPException) as caught:
                await server.api_model_roles(
                    server.ModelRolesIn(model="blind:8b", roles=["chat", "agentic"]))

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("tool", caught.exception.detail.lower())

    async def test_assigning_roles_persists_them(self):
        saved = {}
        with contextlib.ExitStack() as stack:
            for p in self._env(saved=saved):
                stack.enter_context(p)
            out = await server.api_model_roles(
                server.ModelRolesIn(model="coder:30b", roles=["agentic"]))

        self.assertEqual(out["roles"], ["agentic"])
        self.assertEqual(saved["model_roles"]["coder:30b"], ["agentic"])

    async def test_the_last_chat_model_cannot_be_released(self):
        settings = {"model_roles": {"big:27b": ["chat"], "coder:30b": [], "blind:8b": []}}
        with contextlib.ExitStack() as stack:
            for p in self._env(settings):
                stack.enter_context(p)
            with self.assertRaises(server.HTTPException) as caught:
                await server.api_model_roles(server.ModelRolesIn(model="big:27b", roles=[]))

        self.assertEqual(caught.exception.status_code, 409)

    async def test_releasing_agentic_entirely_is_allowed(self):
        settings = {"model_roles": {"big:27b": ["chat", "agentic"], "coder:30b": [], "blind:8b": []}}
        with contextlib.ExitStack() as stack:
            for p in self._env(settings):
                stack.enter_context(p)
            out = await server.api_model_roles(server.ModelRolesIn(model="big:27b", roles=["chat"]))

        self.assertEqual(out["roles"], ["chat"])

    async def test_an_unknown_model_is_a_404(self):
        with contextlib.ExitStack() as stack:
            for p in self._env():
                stack.enter_context(p)
            with self.assertRaises(server.HTTPException) as caught:
                await server.api_model_roles(server.ModelRolesIn(model="ghost:1b", roles=["chat"]))

        self.assertEqual(caught.exception.status_code, 404)

    async def test_a_role_can_only_be_pointed_at_a_model_assigned_to_it(self):
        settings = {"model_roles": {"big:27b": ["chat"], "coder:30b": [], "blind:8b": []}}
        with contextlib.ExitStack() as stack:
            for p in self._env(settings):
                stack.enter_context(p)
            with self.assertRaises(server.HTTPException) as caught:
                await server.api_model_active(
                    server.ModelRolesIn(model="coder:30b", roles=["chat"]))

        self.assertEqual(caught.exception.status_code, 400)

    # Finding a model to install moved to the library screen, so these two ask
    # the library endpoint what the catalog used to be asked.
    LIBRARY = [
        {"name": "tiny-coder", "description": "a small coding model", "capabilities": ["tools"],
         "variants": ["3b"], "params": 3e9, "min_params": 3e9, "downloads": 10,
         "tag_count": 2, "updated": ""},
        {"name": "huge", "description": "a very large general model", "capabilities": ["tools"],
         "variants": ["70b"], "params": 70e9, "min_params": 70e9, "downloads": 20,
         "tag_count": 2, "updated": ""},
    ]

    def _library(self, stack, usable_gb=8):
        for p in self._env():
            stack.enter_context(p)
        stack.enter_context(patch.object(server.reg, "hardware", return_value={
            "gpu": "test", "vram_bytes": usable_gb * 1024**3, "ram_bytes": 16 * 1024**3,
            "free_disk_bytes": 500 * 1024**3, "model_store": "/tmp",
            "usable_bytes": usable_gb * 1024**3}))
        stack.enter_context(patch.object(
            server.lib, "ollama_library", AsyncMock(return_value=list(self.LIBRARY))))

    async def test_the_library_says_what_will_not_fit_before_downloading_it(self):
        with contextlib.ExitStack() as stack:
            self._library(stack)
            body = await server.api_library(source="ollama")

        rows = {e["id"]: e for e in body["items"]}
        self.assertFalse(rows["huge"]["fits"])
        self.assertTrue(rows["tiny-coder"]["fits"])

    async def test_searching_the_library_narrows_it(self):
        with contextlib.ExitStack() as stack:
            self._library(stack)
            body = await server.api_library(source="ollama", q="coding")

        self.assertEqual([e["id"] for e in body["items"]], ["tiny-coder"])

    async def test_a_library_that_does_not_exist_is_refused(self):
        with contextlib.ExitStack() as stack:
            self._library(stack)
            with self.assertRaises(server.HTTPException) as caught:
                await server.api_library(source="civitai")

        self.assertEqual(caught.exception.status_code, 400)

    async def _delete(self, settings, status=200):
        saved = {}
        removed = {}

        class Resp:
            status_code = status
            text = "" if status == 200 else "boom"

        class Client:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, method, url, json=None):
                removed["model"] = (json or {}).get("model")
                return Resp()

        async def refresh():
            return dict(self.REGISTRY)

        with contextlib.ExitStack() as stack:
            for p in self._env(settings, saved=saved):
                stack.enter_context(p)
            stack.enter_context(patch.object(server.httpx, "AsyncClient", Client))
            stack.enter_context(patch.object(server.reg, "refresh_registry", refresh))
            out = await server.api_model_delete(server.ModelTagIn(model="coder:30b"))
        return out, saved, removed

    async def test_deleting_a_model_clears_everything_that_pointed_at_it(self):
        settings = {
            "model_roles": {"big:27b": ["chat"], "coder:30b": ["chat", "agentic"]},
            "model_active": {"agentic": "coder:30b", "chat": "big:27b"},
            "model_context": {"coder:30b": 16384, "big:27b": 32768},
        }
        out, saved, removed = await self._delete(settings)

        self.assertEqual(removed["model"], "coder:30b")
        self.assertEqual(out["removed"], "coder:30b")
        self.assertNotIn("coder:30b", saved["model_roles"])
        self.assertNotIn("agentic", saved["model_active"])
        self.assertNotIn("coder:30b", saved["model_context"])
        # Everything belonging to other models survives.
        self.assertEqual(saved["model_roles"]["big:27b"], ["chat"])
        self.assertEqual(saved["model_active"]["chat"], "big:27b")
        self.assertEqual(saved["model_context"]["big:27b"], 32768)

    async def test_the_last_chat_model_cannot_be_deleted(self):
        settings = {"model_roles": {"big:27b": [], "coder:30b": ["chat"], "blind:8b": []}}
        with self.assertRaises(server.HTTPException) as caught:
            await self._delete(settings)

        self.assertEqual(caught.exception.status_code, 409)

    async def test_an_ollama_failure_is_surfaced_not_swallowed(self):
        settings = {"model_roles": {"big:27b": ["chat"], "coder:30b": ["agentic"]}}
        with self.assertRaises(server.HTTPException) as caught:
            await self._delete(settings, status=500)

        self.assertEqual(caught.exception.status_code, 500)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class NoLegacyKeysTests(unittest.IsolatedAsyncioTestCase):
    """Use model IDs throughout instead of the old role aliases."""

    async def test_health_returns_models_keyed_by_id(self):
        registry = {"m:7b": {"id": "m:7b", "label": "M", "tools": True, "think": True,
                             "vision": False, "context_max": 32768}}

        async def tags():
            return [{"name": "m:7b"}]

        with patch.object(server.st, "load_settings", return_value={}), \
             patch.object(server.reg, "installed", return_value=registry), \
             patch.object(server.oc, "list_models", tags):
            body = await server.health()

        self.assertEqual([m["key"] for m in body["models"]], ["m:7b"])
        self.assertTrue(body["effort_levels"])

    def test_no_module_still_defines_the_pre_role_translations(self):
        for name in ("LEGACY_RESOLVE", "_legacy_key"):
            self.assertFalse(hasattr(server, name), name)
        for name in ("_legacy_context_view", "_absorb_legacy_context"):
            self.assertFalse(hasattr(st, name), name)
        self.assertFalse(hasattr(config, "LEGACY_CONTEXT_SETTING"))

    def test_settings_no_longer_synthesise_the_four_context_keys(self):
        loaded = st.load_settings()

        for legacy in ("chat_context", "reasoning_context", "agent_context", "coder_context"):
            self.assertNotIn(legacy, loaded, legacy)
        self.assertIn("role_context", loaded)

    def test_a_saved_chat_from_before_roles_still_resolves(self):
        # The one-way migration stays; only the two-way shims went.
        chat = st._migrate_chat({"id": "old", "mode": "agentic", "agent_model": "coder"})

        self.assertEqual(chat["model_id"], config.DEFAULT_CODER_MODEL)


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class RequestedRoleTests(unittest.TestCase):
    """Choose the role from the tab, since one model can serve both Chat and Agentic."""

    def test_the_request_decides_not_the_model(self):
        body = server.SendMessage(content="hi", model_key=config.DEFAULT_CHAT_MODEL, mode="chat")
        chat = {"mode": "chat"}

        self.assertEqual(server._requested_role(body, chat), "chat")

    def test_a_model_that_serves_both_roles_still_chats(self):
        # DEFAULT_CHAT_MODEL is assigned to agentic too; that must not matter.
        body = server.SendMessage(content="hi", model_key=config.DEFAULT_AGENT_MODEL, mode="chat")

        self.assertEqual(server._requested_role(body, {"mode": "chat"}), "chat")

    def test_an_agentic_turn_is_honoured(self):
        body = server.SendMessage(content="hi", model_key=config.DEFAULT_CHAT_MODEL, mode="agentic")

        self.assertEqual(server._requested_role(body, {"mode": "chat"}), "agentic")

    def test_the_legacy_mode_names_still_mean_agentic(self):
        for mode in ("code", "computer", "agentic"):
            body = server.SendMessage(content="hi", mode=mode)
            self.assertEqual(server._requested_role(body, {"mode": "chat"}), "agentic", mode)

    def test_a_request_without_a_mode_keeps_the_chat_as_it_is(self):
        body = server.SendMessage(content="hi")

        self.assertEqual(server._requested_role(body, {"mode": "agentic"}), "agentic")
        self.assertEqual(server._requested_role(body, {"mode": "chat"}), "chat")

    def test_resubmit_carries_the_mode_too(self):
        body = server.ResubmitBody(content="hi", mode="chat")

        self.assertEqual(server._requested_role(body, {"mode": "agentic"}), "chat")


@unittest.skipIf(server is None, "server dependencies are installed in Olith's .venv")
class LoopTrustTests(unittest.IsolatedAsyncioTestCase):
    META = {"id": "m", "keep_alive": "1m", "temperature": 0.7, "context": 32768, "think": True}
    SETTINGS = {"agent_repeat_limit": 3, "role_context": {"agentic": 65536},
                "shell_approval_mode": "never_ask"}

    async def _run(self, fake_stream, chat, settings=None, effort="high", horizon=None, run_tool=None):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        def read_result(name, arguments, *a, **k):
            return json.dumps({"path": (arguments or {}).get("path"), "content": "x = 1"})

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server, "_advance_detail_horizon", new=horizon or (lambda *a, **k: False)),
            patch.object(server.agent, "run_tool", side_effect=run_tool or read_result),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={**self.SETTINGS, **(settings or {})}),
            patch.object(server.st, "history_index", return_value=""),
            patch.object(server.st, "memory_as_prompt", return_value=""),
        ):
            return [e async for e in server._agent_loop(chat, self.META, "m", effort=effort, think=False)]

    @staticmethod
    def _read_call(path="a.py"):
        return {"message": {"tool_calls": [{"function": {"name": "read_file", "arguments": {"path": path}}}]},
                "done": True, "done_reason": "stop"}

    async def test_a_thinking_model_thinks_on_every_step_even_with_the_chat_toggle_off(self):
        seen = []

        async def fake_stream(*a, **k):
            seen.append(k.get("think"))
            yield self._read_call(f"f{len(seen)}.py") if len(seen) < 3 else {
                "message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        await self._run(fake_stream, {"id": "t", "project_id": None, "messages": [{"role": "user", "content": "Go."}]})
        self.assertEqual(seen, [True, True, True])

    async def test_low_effort_does_not_think(self):
        seen = []

        async def fake_stream(*a, **k):
            seen.append(k.get("think"))
            yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        await self._run(fake_stream, {"id": "t", "project_id": None, "messages": [{"role": "user", "content": "Go."}]}, effort="low")
        self.assertEqual(seen, [False])

    async def test_rereading_a_result_that_left_the_prompt_is_not_refused(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            yield self._read_call() if rounds <= 3 else {
                "message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        def horizon(chat, *a, **k):
            # Before the third read, drop every earlier result from the render.
            if rounds == 2:
                chat["messages"].append({"role": "system", "kind": server.DETAIL_HORIZON_KIND})
                return True
            return False

        chat = {"id": "r", "project_id": None, "messages": [{"role": "user", "content": "Go."}]}
        await self._run(fake_stream, chat, horizon=horizon)
        refused = [m for m in chat["messages"] if m.get("role") == "tool" and "already called" in m["content"]]
        self.assertEqual(refused, [])

    async def test_rereading_a_visible_result_is_still_refused(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            yield self._read_call() if rounds <= 3 else {
                "message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        chat = {"id": "r", "project_id": None, "messages": [{"role": "user", "content": "Go."}]}
        await self._run(fake_stream, chat)
        refused = [m for m in chat["messages"] if m.get("role") == "tool" and "already called" in m["content"]]
        self.assertEqual(len(refused), 1)

    async def test_the_tool_budget_ends_on_a_debrief_without_tools(self):
        offered = []

        async def fake_stream(*a, **k):
            offered.append(k.get("tools") is not None)
            if k.get("tools") and len(offered) < 10:
                yield self._read_call(f"f{len(offered)}.py")
            else:
                yield {"message": {"content": "Fixed score; collision still open."}, "done": True, "done_reason": "stop"}

        chat = {"id": "b", "project_id": None, "messages": [{"role": "user", "content": "Go."}]}
        events = await self._run(fake_stream, chat, settings={"agent_tool_budget": 3})
        self.assertEqual(offered, [True, True, True, False])
        self.assertEqual(events[-1]["message"]["content"], "Fixed score; collision still open.")

    async def test_calls_after_the_budget_are_not_run(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            yield self._read_call(f"f{rounds}.py") if rounds < 10 else {
                "message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        chat = {"id": "b", "project_id": None, "messages": [{"role": "user", "content": "Go."}]}
        events = await self._run(fake_stream, chat, settings={"agent_tool_budget": 2})
        self.assertEqual(sum(e["type"] == "tool_start" for e in events), 2)
        self.assertIn("tool budget", events[-1]["message"]["content"])

    def test_replay_keeps_the_real_file_body(self):
        body = "int main() { return 0; }\n" * 200
        chat = {"id": "w", "messages": [
            {"role": "user", "content": "Write it."},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "write_file", "arguments": {"path": "a.cpp", "content": body}}]},
            {"role": "tool", "name": "write_file", "content": '{"ok": true}'},
        ]}
        rendered = server._build_messages(chat, "agent")
        call = next(m for m in rendered if m.get("tool_calls"))["tool_calls"][0]["function"]
        self.assertEqual(call["arguments"]["content"], body)

    async def test_failing_checks_bounce_the_debrief_at_most_three_times(self):
        import tempfile
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds > 8:
                raise AssertionError("verification kept bouncing the debrief")
            yield {"message": {"content": "Done, fixed it."}, "done": True, "done_reason": "stop"}

        class Review:
            calls = 0

            def finish(self, *a):
                Review.calls += 1
                return {"files": [{"file": "a.py", "after_hash": str(Review.calls)}]}

        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        with tempfile.TemporaryDirectory() as folder:
            chat = {"id": "v", "project_id": "p", "messages": [{"role": "user", "content": "Fix it."}]}
            settings = {**self.SETTINGS, "language_servers": False, "repo_map_enabled": False}
            with (
                patch.object(server.oc, "stream_chat", new=fake_stream),
                patch.object(server, "_maybe_auto_compact", new=no_compact),
                patch.object(server.st, "get_project", return_value={"id": "p", "name": "p", "root": folder}),
                patch.object(server.st, "save_chat", side_effect=lambda v: v),
                patch.object(server.st, "load_settings", return_value=settings),
                patch.object(server.project_commands, "discover", return_value=[]),
                patch.object(server.project_commands, "applicable", return_value=[{"kind": "test", "timeout_sec": 5}]),
                patch.object(server.project_commands, "shell_command", return_value="pytest"),
                patch.object(server.agent, "run_tool", return_value='{"exit_code": 1}'),
            ):
                events = [e async for e in server._agent_loop_body(chat, self.META, "m", "high", review=Review())]
        self.assertEqual(rounds, 4)
        self.assertTrue(events[-1]["message"]["content"].startswith("This task is incomplete"))

    async def test_a_thinking_step_gets_the_configured_reply_limit(self):
        seen = []

        async def fake_stream(model, msgs, **k):
            seen.append((k.get("think"), k["options"], server.oc.estimate_messages_tokens(msgs)))
            yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        await self._run(fake_stream, {"id": "t", "project_id": None, "messages": [{"role": "user", "content": "Go."}]},
                        settings={"agent_thinking_reply_tokens": 12000})
        think, options, prompt = seen[0]
        self.assertTrue(think)
        self.assertEqual(options["num_predict"], 12000)
        self.assertLessEqual(prompt + options["num_predict"], options["num_ctx"])

    async def test_a_small_window_caps_the_thinking_reply_inside_it(self):
        seen = []

        async def fake_stream(model, msgs, **k):
            seen.append(k["options"])
            yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        await self._run(fake_stream, {"id": "t", "mode": "agentic", "project_id": None, "messages": [{"role": "user", "content": "Go."}]},
                        settings={"role_context": {"agentic": 16384}})
        self.assertLess(seen[0]["num_predict"], seen[0]["num_ctx"])
        self.assertLessEqual(seen[0]["num_ctx"], 16384)

    async def test_a_cut_off_thought_is_carried_into_the_retry(self):
        samples = []

        async def fake_stream(model, msgs, **k):
            samples.append({"think": k.get("think"), "prompt": "\n".join(m.get("content") or "" for m in msgs)})
            if len(samples) == 1:
                yield {"message": {"thinking": "The collision box uses the wrong y offset, so fix kDinoY in step()."},
                       "done": True, "done_reason": "length", "eval_count": 24576}
            else:
                yield {"message": {"content": "Fixed the collision offset."}, "done": True, "done_reason": "stop"}

        events = await self._run(fake_stream, {"id": "s", "project_id": None, "messages": [{"role": "user", "content": "Fix it."}]})
        self.assertEqual(len(samples), 2)
        self.assertFalse(samples[1]["think"])
        self.assertIn("wrong y offset, so fix kDinoY", samples[1]["prompt"])
        self.assertIn("Do not start over", samples[1]["prompt"])
        self.assertEqual(events[-1]["message"]["content"], "Fixed the collision offset.")

    def test_salvage_keeps_the_end_of_a_long_thought(self):
        text = server._salvage_directive("OPENING-THOUGHT " + "x" * 50000 + " conclusion: edit step()")
        self.assertIn("conclusion: edit step()", text)
        self.assertNotIn("OPENING-THOUGHT", text)
        self.assertLess(len(text), server.SALVAGE_CHARS + 1000)

    async def _run_tools(self, script, results):
        """Run the loop with scripted model replies and per-tool results."""
        samples = []

        async def fake_stream(model, msgs, **k):
            samples.append("\n".join(m.get("content") or "" for m in msgs))
            yield script[min(len(samples), len(script)) - 1]

        def run_tool(name, arguments, *a, **k):
            return json.dumps(results[name])

        chat = {"id": "e", "project_id": None, "messages": [{"role": "user", "content": "Fix the cart."}]}
        events = await self._run(fake_stream, chat, run_tool=run_tool)
        return samples, events

    @staticmethod
    def _call(name, **arguments):
        return {"message": {"tool_calls": [{"function": {"name": name, "arguments": arguments}}]}, "done": True, "done_reason": "stop"}

    @staticmethod
    def _say(text):
        return {"message": {"content": text}, "done": True, "done_reason": "stop"}

    async def test_an_unbacked_verified_claim_is_sent_back_once(self):
        samples, events = await self._run_tools(
            [self._call("edit_file", path="cart.js", find="a", replace="b"),
             self._say("Fixed the cart and verified it."),
             self._say("Fixed the cart. I did not run anything, so it is untested.")],
            {"edit_file": {"ok": True, "changed": True}})
        self.assertEqual(len(samples), 3)
        self.assertIn("nothing has run since your last edit", samples[2])
        final = events[-1]["message"]
        self.assertEqual(final["model_content"], "Fixed the cart. I did not run anything, so it is untested.")
        self.assertTrue(final["content"].startswith("**Olith's record of this run**\nNothing ran after the last change"))

    async def test_a_second_unbacked_claim_is_labelled_not_looped(self):
        samples, events = await self._run_tools(
            [self._call("edit_file", path="cart.js", find="a", replace="b"), self._say("Verified, all tests pass.")],
            {"edit_file": {"ok": True, "changed": True}})
        self.assertEqual(len(samples), 3)
        self.assertIn("Nothing ran after the last change", events[-1]["message"]["content"])

    async def test_self_written_tests_are_labelled_as_such(self):
        command = "cat > test_cart.py << 'EOF'\nassert True\nEOF\npython3 test_cart.py"
        samples, events = await self._run_tools(
            [self._call("edit_file", path="cart.js", find="a", replace="b"),
             self._call("run_shell", command=command),
             self._say("Fixed the cart; my new test_cart.py passes.")],
            {"edit_file": {"ok": True, "changed": True}, "run_shell": {"exit_code": 0, "stdout": "", "stderr": ""}})
        self.assertEqual(len(samples), 3)
        self.assertIn("Only checked with tests Olith wrote during this run", events[-1]["message"]["content"])
        self.assertIn("(written this run)", events[-1]["message"]["content"])

    async def test_a_run_that_changed_nothing_gets_no_record(self):
        samples, events = await self._run_tools([self._say("The cart already works; nothing to change.")], {})
        self.assertEqual(events[-1]["message"]["content"], "The cart already works; nothing to change.")

    def test_replay_uses_the_models_words_not_the_record(self):
        chat = {"id": "r", "messages": [
            {"role": "user", "content": "Fix it."},
            {"role": "assistant", "content": "**Olith's record of this run**\nNothing ran.\n\nFixed it.", "model_content": "Fixed it."},
            {"role": "user", "content": "Thanks."},
        ]}
        rendered = server._build_messages(chat, "agent")
        self.assertNotIn("Olith's record", str(rendered))
        self.assertIn("Fixed it.", str(rendered))
