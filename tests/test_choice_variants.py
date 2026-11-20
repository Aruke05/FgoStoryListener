import queue
import threading
import unittest

from fgo_story_listener import DialogueProcessor, choice_display_text


class ChoiceVariantsTests(unittest.TestCase):
    def setUp(self):
        self.out = queue.Queue()
        self.processor = DialogueProcessor(object(), self.out, threading.Event())
        self.addCleanup(self.processor.close)

    def event(self, kind, **data):
        self.processor.notify(dict(type=kind, dialog_id="menu", **data))
        return self.out.get(timeout=2)

    def test_variant_does_not_drop_siblings_or_change_the_selected_index(self):
        raw = ["[&そうだ:そうね]、そう思います。", "どうして？"]
        event = self.event("choices", choices=raw)
        self.assertEqual(event["choices"], ["（そうだ／そうね）、そう思います。", "どうして？"])
        self.assertEqual(event["raw_choices"], raw)
        picked = self.event("choice_selected", index=1, text=raw[1])
        self.assertEqual(picked["text"], raw[1])
        self.event("choice_closed")
        self.assertNotIn("menu", self.processor._active_choices)

    def test_unrecognized_sibling_keeps_its_position(self):
        event = self.event("choices", choices=["[unknown]台詞", "行こう！"])
        self.assertEqual(event["choices"], ["（此项原文暂未识别）", "行こう！"])
        self.assertEqual(choice_display_text("[&はい:はい]。"), "はい。")
        self.assertIsNone(choice_display_text("[&壊れた]。"))
