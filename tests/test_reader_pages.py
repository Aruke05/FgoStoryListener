import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fgo_story_listener import HistoryStore


class ReadingExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = HistoryStore(Path(self.temp.name) / "history.db")
        self.addCleanup(self.store.close)
        self.now = datetime.now()

    def live(self, text, seconds=0, speaker="A", source="ScriptMessageCommonManager.AddText"):
        return self.store.add(speaker, text, source, self.now + timedelta(seconds=seconds), "序章")

    def log(self, lines, folded=None):
        return self.store.add_backlog(
            [{"speaker": "A", "text": line} for line in lines], "序章", self.now, 1000,
            folded_ids=folded,
        )

    def test_reconnect_reuses_translated_page_despite_line_breaks(self):
        page, = self.log(["はじめ。\n続き。"])
        self.store.save_translations([{"id": page, "zh": "开始。继续。"}], "model")
        snapshot = self.live("はじめ。続き。", 60, source="CurrentMessageSnapshot+RenderedText")
        self.assertEqual(snapshot, page)
        self.assertEqual(len(self.store.rows()), 1)
        self.assertEqual(self.store.untranslated_ids(), [])

    def test_fragment_sequence_collapses_and_reuses_translations_in_order(self):
        first, second = self.live("いち。"), self.live("に。", 1)
        self.store.save_translations([{"id": first, "zh": "一。"}, {"id": second, "zh": "二。"}], "model")
        folded = []
        self.log(["いち。\nに。"], folded)
        self.assertCountEqual(folded, [first, second])
        self.assertEqual(self.store.view_counts(include_preloaded=True), (1, 0))
        self.assertEqual([r[6] for r in self.store.rows()], ["一。二。"])
        self.assertEqual(self.store.translation_items([first, second]), [])
        self.assertEqual(self.store.untranslated_ids(), [])
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM dialogue").fetchone()[0], 3)

    def test_no_merge_across_speakers_or_separate_log_occurrences(self):
        self.live("いち。", speaker="B")
        self.live("に。", 1)
        folded = []
        self.log(["いち。\nに。", "いち。\nに。"], folded)
        self.assertEqual(folded, [])
        self.assertEqual(len(self.store.rows()), 4)

    def test_known_page_uses_cache_and_keeps_a_new_spoken_occurrence(self):
        first = self.live("もう一度。")
        self.store.save_translations([{"id": first, "zh": "再来。"}], "model")
        second = self.live("もう一度。", 10)
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.translation_items([second])[0]["translation"], "再来。")
        self.assertEqual(self.store.cached_page_translation("序章", "A", "もう一度。"), "再来。")
        self.assertEqual(self.store.cached_page_translation("序章", "B", "もう一度。"), "")

    def test_upgrade_folds_existing_snapshots_and_keeps_raw_records(self):
        page, = self.log(["第一行。\n第二行。"])
        with self.store._db:
            self.store._db.execute("DROP INDEX dialogue_page_lookup")
            self.store._db.execute(
                "INSERT INTO dialogue(captured_at,quest,speaker,text,raw_speaker,raw_text,source,"
                "translation,translation_status) VALUES(?,?,?,?,?,?,?,?,?)",
                ((self.now + timedelta(minutes=1)).isoformat(), "序章", "A", "第一行。第二行。",
                 "A", "第一行。第二行。", "CurrentMessageSnapshot+RenderedText", "两行。", "done"),
            )
        for _ in range(2):
            reopened = HistoryStore(self.store.path)
            try:
                self.assertEqual([(row[0], row[6]) for row in reopened.rows()], [(page, "两行。")])
                self.assertEqual(reopened._db.execute("SELECT count(*) FROM dialogue").fetchone()[0], 2)
            finally:
                reopened.close()
