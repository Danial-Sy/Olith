# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""A model that generates nothing must say so, not leave a blank bubble."""
import unittest
from unittest.mock import patch

try:
    import server
except ModuleNotFoundError:
    server = None

MID = "hf.co/mradermacher/MeanGirl-i1-GGUF:Q6_K"
META = {"id": MID, "label": MID, "keep_alive": "5m", "think": False}


def _stream(chunks):
    async def fake(*a, **k):
        for c in chunks:
            yield c
    return fake


# The signature Ollama returns for a GGUF with no chat template: the prompt is
# evaluated, then the model emits end-of-sequence and nothing else.
EMPTY = [{"message": {"content": ""}, "done": True,
          "done_reason": "stop", "eval_count": 1, "prompt_eval_count": 32}]
NORMAL = [{"message": {"content": "Hi"}, "done": False},
          {"message": {"content": " there"}, "done": True,
           "done_reason": "stop", "eval_count": 2}]


@unittest.skipIf(server is None, "server deps unavailable")
class EmptyReplyTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, chunks):
        chat = {"id": "c1", "messages": [{"role": "user", "content": "hi"}], "role": "chat"}
        with patch.object(server.oc, "stream_chat", _stream(chunks)), \
             patch.object(server.st, "save_chat"), \
             patch.object(server, "_build_messages", return_value=[{"role": "system", "content": "s"}]), \
             patch.object(server, "_num_ctx_for", return_value=2048), \
             patch.object(server.oc, "model_options", return_value={}), \
             patch.object(server, "_apply_effort", return_value=({}, {"think": False})), \
             patch.object(server, "_resolve_effort", return_value=None):
            return [ev async for ev in server._plain_stream(chat, META, MID)], chat

    async def test_a_model_that_generates_nothing_reports_why(self):
        events, chat = await self._run(EMPTY)
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("without generating anything", events[0]["message"])
        self.assertIn("no chat template", events[0]["message"])

    async def test_the_empty_turn_is_not_persisted(self):
        events, chat = await self._run(EMPTY)
        # Only the user message survives; no blank assistant bubble is stored.
        self.assertEqual([m["role"] for m in chat["messages"]], ["user"])

    async def test_a_normal_reply_is_untouched(self):
        events, chat = await self._run(NORMAL)
        self.assertEqual(events[-1]["type"], "message")
        self.assertEqual(events[-1]["message"]["content"], "Hi there")
        self.assertEqual([m["role"] for m in chat["messages"]], ["user", "assistant"])

    def test_a_generic_empty_response_does_not_blame_the_template(self):
        msg = server._empty_reply_reason(META, {"done_reason": "load", "eval_count": 0})
        self.assertIn("empty response", msg)
        self.assertNotIn("chat template", msg)


if __name__ == "__main__":
    unittest.main()
