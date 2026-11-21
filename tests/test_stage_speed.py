"""Integration regressions for stage2's warm-but-not-yet-played pages."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import queue
import threading

import fgo_story_listener as app
import stage_plan as plan

ANCHORS = ["冒険の始まりですね。", "静かな夜になりました。", "続きはまた明日にしよう。"]
BODY = "そろそろ[r]出発しよう。[wt 0.5][charaFace B 4]時間だ。"
TEXT = "そろそろ\n出発しよう。時間だ。"


def warm(extra=BODY, slots=ANCHORS):
    fragments = [ANCHORS[0], extra, *ANCHORS[1:]]
    script = "".join(f"＠マシュ\n{line}\n[k]\n" for line in fragments)
    return plan.aligned_pages(script, [{"text": s} for s in slots], app.normalize_text, "testscript")


class SpeedTests(TestCase):
    def test_warm_page_requires_authoritative_log(self):
        pages = warm()
        candidate = pages[1]
        self.assertEqual(candidate["text"], TEXT)
        self.assertTrue(candidate["stage_key"].endswith(":log"))
        with TemporaryDirectory() as d:
            store = app.HistoryStore(Path(d)/"test.db")
            try:
                now = datetime.now()
                ids, _ = store.add_preloaded(pages,"章节",now,plan.SOURCE)
                store.save_translations([dict(id=ids[1], zh="准备出发。")],"fixture")
                self.assertFalse(store.cached_page_translation("章节","マシュ",TEXT))
                for name,ja,src in [("マシュ",TEXT,"live"),("マシュ",TEXT[:4],"ScriptBackLog.logData"),
                                    ("別人",TEXT,"ScriptBackLog.logData")]:
                    self.assertIsNone(store.activate_preloaded(name,ja,src,now,"章节",name,ja,1))
                store.add_backlog([dict(speaker="マシュ",text=TEXT)],"章节",now,1)
                item = store.translation_items([ids[1]])[0]
                self.assertFalse(item["preloaded"])
                self.assertEqual(item["translation"],"准备出发。")
            finally:
                store.close()

    def test_dynamic_unknown_duplicate_and_short_evidence_are_not_warmed(self):
        for uncertain in ("[&僕:私]が向かう。","未知の[unknown option]表現。",ANCHORS[0]):
            self.assertFalse(any(x["stage_key"].endswith(":log") for x in warm(uncertain)))
        self.assertFalse(any(x["stage_key"].endswith(":log") for x in warm(slots=ANCHORS[:2])))

    def test_live_arrival_interrupts_background_coalescing(self):
        with TemporaryDirectory() as d, patch.object(app,"data_dir",return_value=Path(d)):
            store = app.HistoryStore(Path(d)/"test.db")
            worker = app.CodexTranslationWorker(store, app.RuntimeConfig(
                translation_engine="exec", translation_debounce_ms=3000), queue.Queue(), "live")
            waiting = threading.Event()
            taken = threading.Event()
            wait = worker.urgent_wakeup.wait
            def watched(timeout):
                waiting.set()
                return wait(timeout)
            def take():
                taken.set()
                worker.stop_event.set()
                return []
            try:
                with patch.object(worker.urgent_wakeup,"wait",side_effect=watched), patch.object(worker,"_take_batch",side_effect=take):
                    worker.enqueue([1])
                    worker.start()
                    self.assertTrue(waiting.wait(2))
                    worker.enqueue_current(2)
                    self.assertTrue(taken.wait(1))
            finally:
                worker.close()
                worker.join(2)
                store.close()

    def test_large_verified_plan_translates_opening_before_bulk(self):
        with TemporaryDirectory() as d, patch.object(app,"data_dir",return_value=Path(d)):
            store = app.HistoryStore(Path(d)/"test.db")
            try:
                records = [dict(speaker="マシュ",text=f"台詞{i}。",
                                stage_key=f"123:digest:{i}") for i in range(117)]
                ids,_ = store.add_preloaded(records,"q",datetime.now(),plan.SOURCE)
                worker = app.CodexTranslationWorker(store, app.RuntimeConfig(),queue.Queue(),"preload")
                worker.enqueue(ids)
                self.assertEqual([r["id"] for r in worker._take_batch()],ids[:6])
                self.assertEqual([r["id"] for r in worker._take_batch()],ids[6:])
                self.assertFalse(worker._take_batch())
            finally:
                store.close()
