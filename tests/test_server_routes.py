# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Route shapes for endpoints that carry a model id in the path."""
import unittest
from unittest.mock import AsyncMock, patch

try:
    import server
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    server = None

# A Hugging Face GGUF pulled through Ollama keeps its repository path, so the
# model id holds slashes. Ollama library tags never do.
HF_ID = "hf.co/mradermacher/MeanGirl-i1-GGUF:Q6_K"
LIBRARY_ID = "qwen3:14b"


@unittest.skipIf(server is None, "server deps unavailable")
class WarmupRouteTests(unittest.TestCase):
    def _warm(self, model_id: str):
        registry = {model_id: {"id": model_id, "label": model_id, "tools": False}}
        with patch.object(server, "_registry", return_value=({}, registry)), \
             patch.object(server, "_warm_one", AsyncMock()) as warm, \
             patch.object(server, "_meta", return_value={"label": model_id}):
            client = TestClient(server.app, base_url="http://localhost")
            res = client.post(f"/api/warmup/model/{model_id}")
        return res, warm

    def test_warms_a_library_tag(self):
        res, warm = self._warm(LIBRARY_ID)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(warm.await_args.args[0], LIBRARY_ID)

    def test_warms_a_hugging_face_id_whose_slashes_are_part_of_the_name(self):
        res, warm = self._warm(HF_ID)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(warm.await_args.args[0], HF_ID)

    def test_warms_a_hugging_face_id_the_browser_percent_encoded(self):
        # encodeURIComponent turns the slashes into %2F; the server decodes the
        # path before matching, so the same id must arrive intact.
        registry = {HF_ID: {"id": HF_ID, "label": HF_ID, "tools": False}}
        quoted = "hf.co%2Fmradermacher%2FMeanGirl-i1-GGUF%3AQ6_K"
        with patch.object(server, "_registry", return_value=({}, registry)), \
             patch.object(server, "_warm_one", AsyncMock()) as warm, \
             patch.object(server, "_meta", return_value={"label": HF_ID}):
            res = TestClient(server.app, base_url="http://localhost").post(
                f"/api/warmup/model/{quoted}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(warm.await_args.args[0], HF_ID)

    def test_an_uninstalled_model_is_still_a_clean_404(self):
        res, warm = self._warm(LIBRARY_ID)  # registry primed with the library tag
        with patch.object(server, "_registry", return_value=({}, {})), \
             patch.object(server, "_warm_one", AsyncMock()):
            res = TestClient(server.app, base_url="http://localhost").post(f"/api/warmup/model/{HF_ID}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["detail"], "Unknown model")


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class ConsentPageTests(unittest.TestCase):
    """The page Aether shows before handing anyone to a login form."""

    def _page(self, cid):
        import asyncio

        from fastapi import HTTPException
        try:
            return asyncio.run(server.api_connect_page(cid)).body.decode()
        except HTTPException as e:
            return e

    def test_it_says_who_is_asking_and_for_what(self):
        page = self._page("google")
        self.assertIn("Aether wants to connect to Google", page)
        self.assertIn("This window belongs to Aether", page)
        self.assertIn("never sees your password", page)

    def test_it_links_on_to_the_real_sign_in(self):
        self.assertIn('href="https://accounts.google.com/ServiceLogin"', self._page("google"))

    def test_it_tells_you_what_to_do_if_you_did_not_ask_for_it(self):
        self.assertIn("If you did not press Connect in Aether", self._page("google"))

    def test_a_service_that_does_not_exist_is_not_a_page(self):
        from fastapi import HTTPException

        self.assertIsInstance(self._page("not-a-service"), HTTPException)


if __name__ == "__main__":
    unittest.main()
