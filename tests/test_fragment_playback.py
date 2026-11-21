"""Event-level regressions for pages assembled from already activated preload rows."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from fgo_story_listener import Application, HistoryStore


class FragmentPlaybackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = HistoryStore(Path(temp.name) / "history.db")
        self.addCleanup(self.store.close)
        self.ui = Mock()
        self.ui._stage_token = None
        for name in ("_finish_fragments", "_wait_for_game_page", "_show_fragment_fallback", "_show_current_row"):
            setattr(self.ui, name, getattr(Application, name).__get__(self.ui))
        self.ui.store = self.store
        self.ui._fragment_rows = []
        self.ui._fragment_timer = None
        self.ui._open_choice_dialog = None
        self.ui._session_anchor = self.ui._next_live_order = 1000
        self.ui.log_window.is_visible.return_value = False
        self.now = datetime.now()
        self.start = int(self.now.timestamp() * 1000)

    def fragment(self, line, offset):
        event = dict(quest="第一幕", speaker="店主", text=line, captured_at_ms=self.start+offset,
                     source="ScriptMessageCommonManager.AddText+DirectManagedText")
        Application._handle_event(self.ui, dict(event, type="dialogue_preview"))
        Application._handle_event(self.ui, dict(event, type="dialogue"))

    def test_one_display_and_no_new_translation_for_three_cached_chunks(self):
        lines = ["そうですね、", "青い箱に", "興味があります。"]
        zh = ["是啊，", "我对蓝盒子", "很感兴趣。"]
        # The preload IDs are allocated before an unrelated LOG page. The
        # fragments are later activated in playback order, not insertion order.
        ids, _ = self.store.add_preloaded(
            [dict(speaker="店主", text=line) for line in reversed(lines)], "第一幕", self.now)
        self.store.save_translations([dict(id=i, zh=t) for i,t in zip(ids, reversed(zh))], "cached")
        self.store.add_backlog([dict(speaker="店主", text="前の台詞。")], "第一幕", self.now, 0)
        for line, offset in zip(lines, [298, 630, 901]):
            self.fragment(line, offset)
        self.ui.overlay.show_line.assert_not_called()
        self.ui.overlay.show_preview.assert_not_called()
        page = lines[0] + lines[1] + "\n" + lines[2]
        Application._handle_event(self.ui, dict(type="backlog", quest="第一幕",
            captured_at_ms=self.start+1006, entries=[dict(speaker="店主", text="前の台詞。"),
                                                   dict(speaker="店主", text=page)]))
        self.ui.overlay.show_line.assert_called_once_with("第一幕", "店主", page, "".join(zh))
        self.ui.translator.enqueue_current.assert_not_called()
        self.assertFalse(self.ui._fragment_rows)
        self.assertEqual(self.store.rows()[-1][6], "".join(zh))

    def test_missing_log_still_shows_and_translates_live_text(self):
        self.fragment("台詞。", 0)
        self.ui._show_fragment_fallback()
        self.ui.overlay.show_line.assert_called_once_with("第一幕", "店主", "台詞。", "")
        self.ui.translator.enqueue_current.assert_called_once_with(self.ui.current_row_id)
