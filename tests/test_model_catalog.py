import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fgo_story_listener as app


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"CODEX_HOME": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cache(self, models):
        (Path(self.temp.name) / "models_cache.json").write_text(
            json.dumps({"models": models}), encoding="utf-8"
        )

    def test_cache_does_not_inject_fixed_models_or_hidden_entries(self):
        self.cache([{"slug": "new-model"}, {"slug": "secret", "visibility": "hide"}])
        self.assertEqual(app.available_codex_models(), ["new-model"])

    def test_missing_cache_does_not_invent_models_or_fast_support(self):
        self.assertEqual(app.available_codex_models(), [])
        self.assertEqual(app.codex_fast_models(), set())

    def test_malformed_cache_is_conservative(self):
        self.cache([None, "bad", {"slug": "ok", "service_tiers": 123,
                                 "supported_reasoning_levels": 123}])
        self.assertEqual(app.available_codex_models(), ["ok"])
        self.assertEqual(app.codex_fast_models(), set())
        self.assertEqual(app.codex_reasoning_efforts("ok"), [])

    def test_live_pages_replace_stale_cache_and_publish_capabilities(self):
        self.cache([{"slug": "stale"}])
        pages = [
            {"result": {"data": [{"model": "future", "serviceTiers": [{"id": "priority"}],
                "supportedReasoningEfforts": [{"reasoningEffort": "ultra"}]}], "nextCursor": "page2"}},
            {"result": {"data": [{"model": "other"}, {"model": "hidden", "hidden": True}], "nextCursor": None}},
        ]
        with patch.object(app.CodexAppServerClient, "start"), patch.object(
            app.CodexAppServerClient, "_request", side_effect=pages
        ) as request, patch.object(app.CodexAppServerClient, "close") as close:
            names, status = app.refresh_codex_models(app.RuntimeConfig())
        self.assertEqual(names, ["future", "other"])
        self.assertIn("model/list", status)
        self.assertEqual(request.call_args_list[1].args[1]["cursor"], "page2")
        close.assert_called_once()
        self.assertEqual(app.available_codex_models(), names)
        self.assertEqual(app.codex_fast_models(), {"future"})
        self.assertEqual(app.codex_reasoning_efforts("future"), ["ultra"])

    def test_refresh_failure_reports_cache_and_closes_client(self):
        self.cache([{"slug": "cached"}])
        with patch.object(app.CodexAppServerClient, "start", side_effect=RuntimeError("offline")), patch.object(
            app.CodexAppServerClient, "close"
        ) as close:
            names, status = app.refresh_codex_models(app.RuntimeConfig())
        self.assertEqual(names, ["cached"])
        self.assertIn("缓存", status)
        self.assertIn("offline", status)
        close.assert_called_once()

    def test_partial_or_looping_pages_never_replace_good_cache(self):
        self.cache([{"slug": "cached"}])
        page = {"result": {"data": [{"model": "partial"}], "nextCursor": "repeat"}}
        with patch.object(app.CodexAppServerClient, "start"), patch.object(
            app.CodexAppServerClient, "_request", return_value=page
        ), patch.object(app.CodexAppServerClient, "close"):
            names, status = app.refresh_codex_models(app.RuntimeConfig())
        self.assertEqual(names, ["cached"])
        self.assertIn("失败", status)


if __name__ == "__main__":
    unittest.main()
