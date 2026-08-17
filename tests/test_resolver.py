from __future__ import annotations

import os
import inspect
import queue
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from fgo_story_listener import (  # noqa: E402
    Application,
    BilingualLogWindow,
    BilingualOverlay,
    CodexExplanationWorker,
    CodexTranslationWorker,
    DialogueProcessor,
    EmulatorBridge,
    HistoryStore,
    JapaneseTextResolver,
    RuntimeConfig,
    StreamingTranslationParser,
    available_codex_models,
    codex_fast_models,
    fast_mode_effective,
    format_bilingual_entry,
    format_entry,
    character_style_guide,
    enforce_character_style_translation,
    managed_text_is_lossless,
    normalize_text,
    quest_label,
    render_explanation_result,
    sanitize_speaker,
    strip_display_tags,
    translation_skip_reason,
)


def known_ocr() -> dict:
    def line(text: str, x: int, y: int) -> dict:
        return {
            "text": " ".join(text),
            "words": [
                {"text": ch, "x": x + i * 70, "y": y, "width": 55, "height": 65}
                for i, ch in enumerate(text)
            ],
        }

    return {
        "width": 2560,
        "height": 1440,
        "lines": [
            line("ダンテ", 70, 1000),
            line("さすがに大きさが違いすきて、", 180, 1165),
            line("戦いにもなっていないよ", 180, 1280),
        ],
    }


class ResolverTests(unittest.TestCase):
    def test_streaming_parser_exposes_prefix_then_completed_objects(self) -> None:
        parser = StreamingTranslationParser()
        self.assertEqual(
            parser.feed(
                '{"translations":[{"id":7,"speaker_zh":"玛修","zh":"你'
            ),
            [],
        )
        self.assertEqual(parser.current_preview(), (7, "你"))
        completed = parser.feed('好\\n前辈。"},{"id":8,"speaker_zh":"","zh":"旁白。"}]')
        self.assertEqual([item["id"] for item in completed], [7, 8])
        self.assertEqual(completed[0]["zh"], "你好\n前辈。")

    def test_streaming_parser_returns_all_one_hundred_rows_incrementally(self) -> None:
        document = __import__("json").dumps(
            {
                "translations": [
                    {"id": index, "speaker_zh": "", "zh": f"译文{index}"}
                    for index in range(1, 101)
                ],
                "memory_updates": [],
                "style_updates": [],
            },
            ensure_ascii=False,
        )
        parser = StreamingTranslationParser()
        completed: list[dict] = []
        for offset in range(0, len(document), 17):
            completed.extend(parser.feed(document[offset:offset + 17]))
        self.assertEqual(len(completed), 100)
        self.assertEqual([item["id"] for item in completed], list(range(1, 101)))

    def test_plain_dialogue_event_does_not_reference_removed_snapshot_flag(self) -> None:
        output: queue.Queue[dict] = queue.Queue()
        stop = threading.Event()
        processor = DialogueProcessor(object(), output, stop)
        try:
            processor.notify(
                {
                    "type": "dialogue",
                    "speaker": "マシュ",
                    "text": "先輩、行きましょう。",
                    "source": "ScriptMessageCommonManager.SetText",
                }
            )
            resolved = output.get(timeout=2)
        finally:
            processor.close()
        self.assertEqual(resolved["speaker"], "マシュ")
        self.assertEqual(resolved["text"], "先輩、行きましょう。")
        self.assertEqual(
            resolved["source"],
            "ScriptMessageCommonManager.SetText+DirectManagedText",
        )
        self.assertNotIn("is_snapshot", str(resolved))

    def test_add_text_preview_is_lossless_but_not_a_database_dialogue(self) -> None:
        output: queue.Queue[dict] = queue.Queue()
        processor = DialogueProcessor(object(), output, threading.Event())
        try:
            processor.notify(
                {
                    "type": "dialogue_preview",
                    "speaker": "マシュ",
                    "text": "先輩、\r\n次のページです。",
                    "source": "ScriptMessageCommonManager.AddText+Preview",
                }
            )
            resolved = output.get(timeout=2)
        finally:
            processor.close()
        self.assertEqual(resolved["type"], "dialogue_preview")
        self.assertEqual(resolved["text"], "先輩、\n次のページです。")
        self.assertEqual(
            resolved["source"],
            "ScriptMessageCommonManager.AddText+Preview+DirectManagedText",
        )

    def test_main_window_never_uses_topmost_or_forced_focus(self) -> None:
        show_log_source = inspect.getsource(Application.show_log)
        init_source = inspect.getsource(Application.__init__)
        overlay_source = inspect.getsource(BilingualOverlay)
        backlog_source = inspect.getsource(BilingualLogWindow)
        self.assertNotIn("focus_force", show_log_source)
        self.assertNotIn("-topmost", show_log_source)
        self.assertNotIn("self.deiconify", show_log_source)
        self.assertIn("self.log_window.open", show_log_source)
        self.assertNotIn('bind("<Map>"', init_source)
        self.assertNotIn('attributes("-topmost", True)', overlay_source)
        self.assertNotIn('attributes("-topmost", True)', backlog_source)
        self.assertIn("self.lift()", backlog_source)
        self.assertIn("self.focus_set()", backlog_source)
        self.assertIn("self.source_label.configure(wraplength=width)", overlay_source)
        self.assertIn("self.card.winfo_width() - 42", overlay_source)
        self.assertNotIn("self.winfo_width() - 82", overlay_source)

    def test_live_backlog_and_scenario_events_never_rebuild_the_full_tree(self) -> None:
        source = inspect.getsource(Application._handle_event)
        scenario = source.split('elif kind == "scenario_plan":', 1)[1].split(
            'elif kind == "backlog":', 1
        )[0]
        backlog = source.split('elif kind == "backlog":', 1)[1].split(
            'elif kind == "choices":', 1
        )[0]
        self.assertNotIn("self._apply_filter()", scenario)
        self.assertNotIn("self._apply_filter()", backlog)
        self.assertIn("self._update_tree_rows", scenario)
        self.assertIn("self._update_tree_rows", backlog)

    def test_scenario_pretranslation_hook_keeps_live_fallback_and_real_signature(self) -> None:
        hook_source = (PROJECT / "hook.js").read_text(encoding="utf-8")
        self.assertIn("hook('ScriptManager', 'AnalysText', 11", hook_source)
        self.assertIn("hook('ScriptManager', 'AnalysScript', 2", hook_source)
        self.assertIn("snapshotScenarioPlan(scriptManager)", hook_source)
        self.assertNotIn("executeMessageFlagList: fieldOffset", hook_source)
        self.assertIn("kind: 'dialogue_fragment'", hook_source)
        self.assertIn("source: 'ScriptManager.VisibleTextSlots'", hook_source)
        self.assertIn("if (tag) continue", hook_source)
        self.assertIn("normal$|select$", hook_source)
        # Choice labels are only reliable when ScriptSelectDialog materializes
        # its managed string array, so they intentionally take the live path.
        self.assertIn("hook('ScriptSelectDialog', 'Open', 3", hook_source)
        # Existing live capture remains independent of the optional preload hooks.
        self.assertIn("hook('ScriptMessageCommonManager', 'SetText', 1", hook_source)
        self.assertIn("hook('ScriptMessageCommonManager', 'AddText', 3", hook_source)

    def test_hook_waits_for_libil2cpp_instead_of_failing_during_startup(self) -> None:
        hook_source = (PROJECT / "hook.js").read_text(encoding="utf-8")
        self.assertIn("Process.findModuleByName('libil2cpp.so')", hook_source)
        self.assertNotIn("Process.getModuleByName('libil2cpp.so')", hook_source)
        self.assertIn("function scheduleInstallRetry(reason)", hook_source)
        self.assertIn("state.installComplete = true", hook_source)
        self.assertIn("IL2CPP domain 尚未初始化", hook_source)
        self.assertIn("Assembly-CSharp.dll 尚未加载", hook_source)

    def test_runtime_has_no_screenshot_or_ocr_path(self) -> None:
        processor_source = inspect.getsource(DialogueProcessor)
        bridge_source = inspect.getsource(EmulatorBridge)
        spec_source = (PROJECT / "FgoStoryListener.spec").read_text(encoding="utf-8")
        self.assertNotIn("WindowsJapaneseOcr", processor_source)
        self.assertNotIn("capture_screen", processor_source)
        self.assertNotIn("screencap", bridge_source)
        self.assertNotIn("ocr_helper", spec_source)
        self.assertFalse((PROJECT / "ocr_helper.ps1").exists())
        self.assertIn("ScriptBackLog.logData", processor_source)

    def test_rendered_managed_text_uses_lossless_fast_path(self) -> None:
        raw = "[line 3]人が、人の[#方法:かたち]で、[r]理に[#適:かな]っている。[r]"
        self.assertEqual(normalize_text(raw), "人が、人の方法で、\n理に適っている。")
        self.assertTrue(managed_text_is_lossless(raw))
        wrapped = "[#父親の目が鋭くてな]。[r]消去法で私しかいない、という訳だ。[r]"
        self.assertEqual(
            normalize_text(wrapped),
            "父親の目が鋭くてな。\n消去法で私しかいない、という訳だ。",
        )
        self.assertEqual(strip_display_tags(wrapped), "父親の目が鋭くてな。\n消去法で私しかいない、という訳だ。\n")
        self.assertTrue(managed_text_is_lossless(wrapped))
        self.assertFalse(managed_text_is_lossless("母件氾引勻凶仁"))
        self.assertFalse(managed_text_is_lossless("[unknown]台詞"))

    def test_verified_page_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            resolver = JapaneseTextResolver()
            speaker_ocr, text_ocr = resolver.extract_story(known_ocr())
            raw_speaker = "母件氾"
            raw_text = "今允互卞湮五今互綃中允亢化﹜[r]煾中卞手卅勻化中卅中方＃＃﹝[r]"

            self.assertEqual(resolver.resolve(raw_speaker, speaker_ocr), "ダンテ")
            self.assertEqual(
                resolver.resolve(raw_text, text_ocr),
                "さすがに大きさが違いすぎて、\n戦いにもなっていないよ……。",
            )

    def test_display_line_break(self) -> None:
        self.assertEqual(strip_display_tags("abc[r]def[n]ghi"), "abc\ndef\nghi")

    def test_already_rendered_managed_text_is_not_decoded_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            resolver = JapaneseTextResolver()
            self.assertEqual(
                resolver.resolve("今日は楽しかったよ！[r]", "今日は楽しかったよ!"),
                "今日は楽しかったよ！",
            )
            self.assertEqual(resolver.decode_known("今日は楽しかったよ！[r]"), "今日は楽しかったよ！")

    def test_choice_stack_ignores_background_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            resolver = JapaneseTextResolver()
            ocr = {
                "width": 2560,
                "height": 1440,
                "lines": [
                    known_ocr()["lines"][0],
                    {
                        "text": "ど う す る ？",
                        "words": [{"x": 950, "y": 430, "width": 620, "height": 60}],
                    },
                    {
                        "text": "戻 る",
                        "words": [{"x": 1150, "y": 650, "width": 260, "height": 60}],
                    },
                    known_ocr()["lines"][1],
                ],
            }
            self.assertEqual(
                resolver.resolve_choices(["どうする？", "戻る"], ocr),
                ["どうする？", "戻る"],
            )

    def test_speaker_artifact_and_quest_fallback(self) -> None:
        self.assertEqual(sanitize_speaker("耀ナッちゃん"), "ナッちゃん")
        self.assertEqual(sanitize_speaker("耀星のハサン"), "耀星のハサン")
        self.assertEqual(
            quest_label(
                {
                    "title": "Dummy",
                    "quest_id": 94159003,
                    "phase": 0,
                    "script": "AssetData 9415900321",
                }
            ),
            "关卡 94159003 · 剧情 21",
        )

    def test_backlog_is_ordered_before_live_and_filterable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            anchor = store.max_order() + 1_000_000
            inserted = store.add_backlog(
                [
                    {"speaker": "マシュ", "text": "前の一行"},
                    {"speaker": "【已选择】", "text": "戻る"},
                ],
                "关卡 1",
                datetime.now(),
                anchor,
            )
            self.assertEqual(len(inserted), 2)
            store.add("ナッちゃん", "今の一行", "live", datetime.now(), "关卡 1",
                      display_order=anchor)
            store.add("マシュ", "別の关卡", "live", datetime.now(), "关卡 2",
                      display_order=anchor + 10)
            self.assertEqual(
                [row[4] for row in store.rows("关卡 1")],
                ["前の一行", "戻る", "今の一行"],
            )
            self.assertEqual(store.quests(), ["关卡 1", "关卡 2"])
            store.close()

    def test_full_backlog_reconciles_an_existing_live_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            anchor = store.max_order() + 1_000_000
            store.add(
                "ナッちゃん", "今日は楽しかったよ！", "live", datetime.now(),
                "第3節", display_order=anchor,
            )
            inserted = store.add_backlog(
                [
                    {"speaker": "オルガマリー", "text": "そうね。"},
                    {"speaker": "ナッちゃん", "text": "今日は楽しかったよ！"},
                ],
                "第3節",
                datetime.now(),
                anchor + 1_000_000,
            )
            self.assertEqual(len(inserted), 1)
            self.assertEqual(
                [(row[3], row[4]) for row in store.rows("第3節")],
                [
                    ("オルガマリー", "そうね。"),
                    ("ナッちゃん", "今日は楽しかったよ！"),
                ],
            )
            store.close()

    def test_new_phase_backlog_stays_after_previous_phase_ending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            anchor = store.max_order() + 1_000_000
            store.add(
                "紅閻魔", "第一阶段最后一句", "live", now,
                "第4節（阶段 1）", display_order=anchor,
            )
            store.add(
                "【已选择】", "1. 前进", "live", now,
                "第4節（阶段 1）", display_order=anchor + 10,
            )
            store.add(
                "【已选择】", "1. 制造百鬼夜行", "live", now,
                "第4節（阶段 2）", display_order=anchor + 20,
            )
            inserted = store.add_backlog(
                [
                    {"speaker": "小野小町", "text": "第二阶段第一句"},
                    {"speaker": "小野小町", "text": "第二阶段当前句"},
                ],
                "第4節（阶段 2）",
                now,
                anchor,
            )
            self.assertEqual(len(inserted), 2)
            self.assertEqual(
                [(row[2], row[4]) for row in store.rows()],
                [
                    ("第4節（阶段 1）", "第一阶段最后一句"),
                    ("第4節（阶段 1）", "1. 前进"),
                    ("第4節（阶段 2）", "第二阶段第一句"),
                    ("第4節（阶段 2）", "第二阶段当前句"),
                    ("第4節（阶段 2）", "1. 制造百鬼夜行"),
                ],
            )
            self.assertEqual(
                store.latest_visible_id(
                    "第4節（阶段 2）", "小野小町", "第二阶段当前句"
                ),
                inserted[-1],
            )
            store.close()

    def test_backlog_repairs_missing_live_speaker_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            anchor = store.max_order() + 1_000_000
            live_id = store.add(
                "", "今日は楽しかったよ！", "ScriptMessageCommonManager.SetText",
                now, "第3節", display_order=anchor,
            )
            self.assertIsNotNone(live_id)
            inserted = store.add_backlog(
                [{"speaker": "ナッちゃん", "text": "今日は楽しかったよ！"}],
                "第3節", now, anchor + 1_000_000,
            )
            self.assertEqual(inserted, [])
            rows = store.rows("第3節")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "ナッちゃん")
            self.assertEqual(rows[0][5], "ScriptBackLog.logData")
            store.close()

    def test_named_live_event_repairs_recent_unnamed_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            row_id = store.add("", "同じ台詞", "live-before-name", now, "关卡")
            self.assertIsNotNone(row_id)
            repaired_id = store.add("マシュ", "同じ台詞", "live-with-name", now, "关卡")
            self.assertEqual(repaired_id, row_id)
            rows = store.rows("关卡")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "マシュ")
            store.close()

    def test_authoritative_narration_clears_a_stale_live_speaker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            anchor = store.max_order() + 1_000_000
            live_id = store.add(
                "マシュ", "静かな夜だった。", "ScriptMessageCommonManager.SetText",
                now, "序章", display_order=anchor,
            )
            inserted = store.add_backlog(
                [{"speaker": "", "text": "静かな夜だった。"}],
                "序章", now, anchor + 1_000_000,
            )
            self.assertEqual(inserted, [])
            rows = store.rows("序章")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], live_id)
            self.assertEqual(rows[0][3], "")
            self.assertEqual(rows[0][5], "ScriptBackLog.logData")
            self.assertEqual(format_entry(rows[0][3], rows[0][4]), "静かな夜だった。")
            store.close()

    def test_late_live_name_does_not_overwrite_authoritative_narration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            inserted = store.add_backlog(
                [{"speaker": "", "text": "風が吹き抜けた。"}],
                "序章", now, store.max_order() + 1_000_000,
            )
            self.assertEqual(len(inserted), 1)
            duplicate = store.add(
                "マシュ", "風が吹き抜けた。", "ScriptMessageCommonManager.SetText",
                now, "序章",
            )
            self.assertIsNone(duplicate)
            rows = store.rows("序章")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][3], "")
            store.close()

    def test_live_event_arriving_after_backlog_is_still_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            inserted = store.add_backlog(
                [{"speaker": "アズライール", "text": "父親の目が鋭くてな。\n消去法だ。"}],
                "第4節",
                now,
                store.max_order() + 1_000_000,
            )
            self.assertEqual(len(inserted), 1)
            duplicate = store.add(
                "アズライール",
                "父親の目が鋭くてな。\n消去法だ。",
                "ScriptMessageCommonManager.AddText",
                now,
                "第4節",
                display_order=store.max_order() + 1_000_000,
            )
            self.assertIsNone(duplicate)
            self.assertEqual(len(store.rows()), 1)
            store.close()

    def test_old_bare_hash_ocr_fragment_is_repaired_and_merged_with_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.db"
            store = HistoryStore(path)
            now = datetime.now().isoformat(timespec="milliseconds")
            raw = "[#父親の目が鋭くてな]。[r]消去法で私しかいない、という訳だ。[r]"
            full = "父親の目が鋭くてな。\n消去法で私しかいない、という訳だ。"
            store._db.execute(
                "INSERT INTO dialogue "
                "(captured_at,display_order,quest,speaker,text,raw_speaker,raw_text,source," 
                "translation,translation_model,translation_status,translation_updated_at,preloaded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    now, 1000, "第4節", "アズライール", full,
                    "アズライール", full, "ScriptBackLog.logData",
                    "父亲的目光很锐利。\n用排除法来看，只剩下我。", "test", "done", now,
                ),
            )
            store._db.execute(
                "INSERT INTO dialogue "
                "(captured_at,display_order,quest,speaker,text,raw_speaker,raw_text,source," 
                "translation,translation_model,translation_status,translation_updated_at,preloaded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (
                    now, 2000, "第4節", "アズライール",
                    "。\n消去法で私しかいない、という訳だ。",
                    "アズライール", raw,
                    "ScriptMessageCommonManager.AddText+ScreenOCRv2",
                    "。\n用排除法来看，只剩下我。", "test", "done", now,
                ),
            )
            store._db.commit()
            store.close()
            repaired = HistoryStore(path)
            rows = repaired.rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][4], full)
            self.assertTrue(rows[0][5].startswith("ScriptBackLog"))
            self.assertTrue((Path(temp) / "history-before-v2.5.1.db").is_file())
            repaired.close()

    def test_incremental_backlog_preserves_repeated_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            anchor = store.max_order() + 1_000_000
            first = [
                {"speaker": "A", "text": "……"},
                {"speaker": "B", "text": "次"},
            ]
            self.assertEqual(len(store.add_backlog(first, "关卡", datetime.now(), anchor)), 2)
            second = first + [{"speaker": "A", "text": "……"}]
            self.assertEqual(len(store.add_backlog(second, "关卡", datetime.now(), anchor)), 1)
            self.assertEqual([row[4] for row in store.rows("关卡")], ["……", "次", "……"])
            store.close()

    def test_translation_columns_and_quest_memory_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            row_id = store.add("マシュ", "先輩！", "live", datetime.now(), "序章")
            self.assertIsNotNone(row_id)
            self.assertEqual(store.untranslated_ids(), [row_id])
            store.mark_translating([row_id])
            saved = store.save_translations([{"id": row_id, "zh": "前辈！"}], "test-model")
            self.assertEqual(saved, [{"id": row_id, "translation": "前辈！"}])
            row = store.rows()[0]
            self.assertEqual(row[6], "前辈！")
            self.assertEqual(row[7], "test-model")
            self.assertEqual(row[8], "done")
            store.save_quest_memory(
                "序章",
                [{"key": "先輩", "value": "玛修称主人公为前辈", "evidence_line_ids": [row_id]}],
            )
            self.assertEqual(store.quest_memory("序章")[0]["key"], "先輩")
            store.close()

    def test_model_speaker_translation_is_cached_per_quest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            row_id = store.add(
                "ナッちゃん", "今日は楽しかったよ！", "live", datetime.now(), "第3節"
            )
            assert row_id
            saved = store.save_translations(
                [
                    {
                        "id": row_id,
                        "speaker_zh": "小娜",
                        "zh": "今天很开心！",
                    }
                ],
                "test-model",
            )
            self.assertEqual(saved[0]["speaker_translation"], "小娜")
            self.assertEqual(store.speaker_translation("第3節", "ナッちゃん"), "小娜")
            self.assertEqual(store.speaker_translation("别的关卡", "ナッちゃん"), "")
            store.close()

    def test_translation_schema_requires_speaker_translation(self) -> None:
        schema = __import__("json").loads(
            (PROJECT / "translation_runtime" / "translation_schema.json").read_text(
                encoding="utf-8"
            )
        )
        translation_item = schema["properties"]["translations"]["items"]
        self.assertIn("speaker_zh", translation_item["required"])

    def test_runtime_config_and_codex_model_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp, "CODEX_HOME": temp}
        ):
            (Path(temp) / "models_cache.json").write_text(
                '{"models":['
                '{"slug":"custom-fgo-model","visibility":"list"},'
                '{"slug":"gpt-5.5","visibility":"list",'
                '"service_tiers":[{"id":"priority","name":"Fast"}]}'
                ']}',
                encoding="utf-8",
            )
            config = RuntimeConfig(
                translation_model="custom-fgo-model",
                translation_batch_size=55,
                pretranslate_scenario=False,
            )
            config.save()
            loaded = RuntimeConfig.load()
            self.assertEqual(loaded.translation_model, "custom-fgo-model")
            self.assertEqual(loaded.preload_translation_model, "gpt-5.6-sol")
            self.assertEqual(loaded.preload_translation_reasoning, "high")
            self.assertEqual(loaded.translation_batch_size, 55)
            self.assertEqual(loaded.translation_engine, "app-server")
            self.assertFalse(loaded.pretranslate_scenario)
            models = available_codex_models()
            self.assertEqual(models[0], "gpt-5.6-terra")
            self.assertIn("custom-fgo-model", models)
            self.assertEqual(codex_fast_models(), {"gpt-5.5"})
            self.assertTrue(fast_mode_effective(RuntimeConfig(translation_model="gpt-5.5")))
            self.assertFalse(
                fast_mode_effective(RuntimeConfig(translation_model="custom-fgo-model"))
            )

    def test_newer_codex_model_error_retries_with_compatible_default(self) -> None:
        class RejectingClient:
            def translate(self, *args, **kwargs):
                raise RuntimeError(
                    "The 'gpt-5.6-sol' model requires a newer version of Codex."
                )

            def close(self):
                return None

        class CompatibleClient:
            def __init__(self, config):
                self.config = config

            def translate(self, *args, **kwargs):
                return {
                    "translations": [{"id": 1, "speaker_zh": "玛修", "zh": "你好。"}],
                    "memory_updates": [],
                    "style_updates": [],
                }

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp, "CODEX_HOME": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            config = RuntimeConfig(translation_model="gpt-5.6-sol")
            events = __import__("queue").Queue()
            worker = CodexTranslationWorker(store, config, events)
            worker._app_server = RejectingClient()
            with patch(
                "fgo_story_listener.CodexAppServerClient", CompatibleClient
            ), patch.object(worker, "_invoke_exec") as exec_fallback:
                result = worker._invoke(
                    "第4节",
                    '{"lines_to_translate":[{"id":1,"ja":"こんにちは。"}]}',
                )
            self.assertEqual(config.translation_model, "gpt-5.5")
            self.assertEqual(result["translations"][0]["zh"], "你好。")
            exec_fallback.assert_not_called()
            self.assertIn("已自动切换为 gpt-5.5", events.get_nowait()["text"])
            self.assertEqual(RuntimeConfig.load().translation_model, "gpt-5.5")
            store.close()

    def test_old_single_sol_profile_migrates_to_split_live_and_preload_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            app_dir = Path(temp) / "FgoStoryListener"
            app_dir.mkdir(parents=True)
            (app_dir / "config.json").write_text(
                '{"translation_model":"gpt-5.6-sol",'
                '"translation_reasoning":"high",'
                '"translation_fast_mode":true}',
                encoding="utf-8",
            )
            loaded = RuntimeConfig.load()
            self.assertEqual(loaded.translation_model, "gpt-5.6-terra")
            self.assertEqual(loaded.translation_reasoning, "low")
            self.assertEqual(loaded.preload_translation_model, "gpt-5.6-sol")
            self.assertEqual(loaded.preload_translation_reasoning, "high")

    def test_retry_includes_only_latest_hidden_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            old_ids, _ = store.add_preloaded(
                [{"speaker": "", "text": "旧阶段隐藏文本。"}],
                "阶段 1",
                datetime(2026, 8, 15, 1, 0, 0),
            )
            new_ids, _ = store.add_preloaded(
                [{"speaker": "", "text": "当前阶段隐藏文本。"}],
                "阶段 2",
                datetime(2026, 8, 15, 2, 0, 0),
            )
            visible_id = store.add(
                "マシュ", "当前可见文本。", "SetText", datetime.now(), "阶段 2"
            )
            store.mark_translation_error([*old_ids, *new_ids, visible_id])
            self.assertEqual(store.latest_preloaded_untranslated_ids(), new_ids)
            retried = store.reset_translation_errors()
            self.assertEqual(set(retried), {new_ids[0], visible_id})
            self.assertEqual(store.translation_items(old_ids)[0]["status"], "error")
            store.close()

    def test_pretranslated_scenario_stays_hidden_until_exact_live_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            pending, total = store.add_preloaded(
                [
                    {"speaker": "マシュ", "text": "先輩、行きましょう。"},
                    {"speaker": "", "text": "静かな夜だった。"},
                ],
                "序章",
                datetime.now(),
            )
            self.assertEqual(total, 2)
            self.assertEqual(len(pending), 2)
            self.assertEqual(store.rows(), [])
            self.assertEqual(store.quests(), [])
            preload_worker = CodexTranslationWorker(
                store, RuntimeConfig(), __import__("queue").Queue()
            )
            preload_prompt = preload_worker._build_prompt(
                store.translation_items(pending)
            )
            self.assertIn('"scenario_preload_mode": true', preload_prompt)
            store.save_translations(
                [
                    {"id": pending[0], "zh": "前辈，我们走吧。"},
                    {"id": pending[1], "zh": "那是一个寂静的夜晚。"},
                ],
                "test-model",
            )
            activated = store.activate_preloaded(
                "マシュ",
                "先輩、行きましょう。",
                "ScriptLineMessage.SetText",
                datetime.now(),
                "序章",
                "マシュ",
                "先輩、行きましょう。",
                10,
            )
            self.assertEqual(activated, pending[0])
            self.assertEqual(len(store.rows()), 1)
            self.assertEqual(store.rows()[0][6], "前辈，我们走吧。")
            self.assertEqual(store.quests(), ["序章"])
            # A different line or branch is not exposed merely because it was
            # pretranslated in the parsed scenario arrays.
            self.assertIsNone(
                store.activate_preloaded(
                    "マシュ", "別の台詞", "live", datetime.now(), "序章", "", "", 20
                )
            )
            self.assertEqual(len(store.rows()), 1)
            store.close()

    def test_translated_visible_fragments_activate_as_one_complete_log_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            pending, total = store.add_preloaded(
                [
                    {"speaker": "", "text": "本来は罪人について書かれたものですが、"},
                    {"speaker": "", "text": "魂の処遇を決める意味では同じです。"},
                ],
                "第4節（阶段 2）",
                now,
                "ScriptManager.VisibleTextSlots+DirectManagedText",
            )
            self.assertEqual(total, 2)
            store.save_translations(
                [
                    {"id": pending[0], "zh": "本来记录的是罪人与罪行，"},
                    {"id": pending[1], "zh": "但在决定灵魂去处这一点上是相同的。"},
                ],
                "test-model",
            )
            activated = store.activate_preloaded(
                "小野小町",
                "本来は罪人について書かれたものですが、\n魂の処遇を決める意味では同じです。",
                "ScriptBackLog.logData",
                now,
                "第4節（阶段 2）",
                "小野小町",
                "本来は罪人について書かれたものですが、\n魂の処遇を決める意味では同じです。",
                1_000_000,
            )
            self.assertEqual(activated, pending[0])
            visible = store.translation_items([activated])
            self.assertEqual(
                visible[0]["translation"],
                "本来记录的是罪人与罪行，但在决定灵魂去处这一点上是相同的。",
            )
            self.assertFalse(visible[0]["preloaded"])
            self.assertEqual(len(store.rows("第4節（阶段 2）")), 1)
            store.close()

    def test_long_fragment_page_still_hits_completed_preload_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            fragments = [f"断片{i}。" for i in range(12)]
            pending, _ = store.add_preloaded(
                [{"speaker": "", "text": value} for value in fragments],
                "长分页关卡",
                now,
            )
            store.save_translations(
                [{"id": row_id, "zh": f"片段{i}。"} for i, row_id in enumerate(pending)],
                "test-model",
            )
            activated = store.activate_preloaded(
                "マシュ", "".join(fragments), "SetText", now, "长分页关卡",
                "マシュ", "".join(fragments), 100,
            )
            self.assertEqual(activated, pending[0])
            self.assertEqual(
                store.translation_items([activated])[0]["translation"],
                "".join(f"片段{i}。" for i in range(12)),
            )
            store.close()

    def test_backlog_activates_pretranslated_exact_and_fragment_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            pending, total = store.add_preloaded(
                [
                    {"speaker": "错误人物", "text": "閻魔帳、というものがあるそうですね。"},
                    {
                        "speaker": "错误人物",
                        "text": "本来は罪人や罪について書かれたもののようですが、",
                    },
                    {
                        "speaker": "错误人物",
                        "text": "魂の処遇を決めるもの、という意味では同じなので……",
                    },
                ],
                "第4节（阶段 2）",
                datetime.now(),
            )
            self.assertEqual(total, 3)
            store.save_translations(
                [
                    {"id": pending[0], "speaker_zh": "", "zh": "听说有一种叫作阎魔账的东西呢。"},
                    {"id": pending[1], "speaker_zh": "", "zh": "原本似乎是用来记录罪人与罪行的，"},
                    {
                        "id": pending[2],
                        "speaker_zh": "",
                        "zh": "但就决定灵魂如何处置这一点而言，两者是一样的……",
                    },
                ],
                "gpt-5.6-sol",
            )
            to_translate = store.add_backlog(
                [
                    {"speaker": "小野小町", "text": "閻魔帳、というものがあるそうですね。"},
                    {
                        "speaker": "小野小町",
                        "text": (
                            "本来は罪人や罪について書かれたもののようですが、\n"
                            "魂の処遇を決めるもの、という意味では同じなので……"
                        ),
                    },
                ],
                "第4节（阶段 2）",
                datetime.now(),
                1_000_000,
            )
            self.assertEqual(to_translate, [])
            rows = store.rows("第4节（阶段 2）")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][3], "小野小町")
            self.assertEqual(rows[0][6], "听说有一种叫作阎魔账的东西呢。")
            self.assertEqual(
                rows[1][6],
                "原本似乎是用来记录罪人与罪行的，"
                "但就决定灵魂如何处置这一点而言，两者是一样的……",
            )
            self.assertTrue(all(row[5] == "ScriptBackLog.logData" for row in rows))
            self.assertEqual(
                store._db.execute(
                    "SELECT COUNT(*) FROM dialogue WHERE preloaded = 1"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_database_view_exposes_preloaded_rows_without_polluting_log_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            store.add("マシュ", "已播放。", "live", datetime.now(), "阶段")
            pending, _ = store.add_preloaded(
                [{"speaker": "ダンテ", "text": "后台预载。"}],
                "阶段",
                datetime.now(),
            )
            self.assertEqual(len(store.rows("阶段")), 1)
            view = store.view_rows("阶段", include_preloaded=True)
            self.assertEqual(len(view), 2)
            self.assertEqual([bool(row[10]) for row in view], [False, True])
            self.assertIn(pending[0], [int(row[0]) for row in view])
            self.assertEqual(store.quests(include_preloaded=True), ["阶段"])
            store.close()

    def test_v268_artificial_fragment_newlines_are_repaired_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.db"
            store = HistoryStore(path)
            row_id = store.add(
                "小野小町",
                "……作らせていただきました！\nこれが『百鬼夜行閻魔帳』となります！",
                "ScriptBackLog.logData",
                datetime.fromisoformat("2026-08-15T05:30:00"),
                "阶段",
            )
            store._db.execute(
                "UPDATE dialogue SET translation = ?, translation_model = ?, "
                "translation_status = 'done', translation_updated_at = ? WHERE id = ?",
                (
                    "……制作出了这个！\n这就是《\n百鬼夜行\n阎魔账》！",
                    "gpt-5.6-sol",
                    "2026-08-15T05:20:00",
                    row_id,
                ),
            )
            store._db.commit()
            store.close()
            reopened = HistoryStore(path)
            self.assertEqual(
                reopened.rows("阶段")[0][6],
                "……制作出了这个！这就是《百鬼夜行阎魔账》！",
            )
            self.assertTrue((Path(temp) / "history-before-v2.6.9.db").is_file())
            reopened.close()

    def test_backlog_returns_untranslated_activated_preload_for_queueing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            pending, _ = store.add_preloaded(
                [{"speaker": "", "text": "まだ翻訳されていない。"}],
                "阶段",
                datetime.now(),
            )
            to_translate = store.add_backlog(
                [{"speaker": "マシュ", "text": "まだ翻訳されていない。"}],
                "阶段",
                datetime.now(),
                1_000_000,
            )
            self.assertEqual(to_translate, pending)
            item = store.translation_items(pending)[0]
            self.assertFalse(item["preloaded"])
            self.assertEqual(item["speaker"], "マシュ")
            store.close()

    def test_untranslated_fragments_are_not_fuzzily_activated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            now = datetime.now()
            store.add_preloaded(
                [{"speaker": "", "text": "前半"}, {"speaker": "", "text": "后半"}],
                "关卡",
                now,
                "ScriptManager.VisibleTextSlots+DirectManagedText",
            )
            self.assertIsNone(
                store.activate_preloaded(
                    "", "前半\n后半", "live", now, "关卡", "", "前半\n后半", 100
                )
            )
            store.close()

    def test_preloaded_speaker_survives_an_unnamed_live_activation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            pending, total = store.add_preloaded(
                [{"speaker": "マシュ", "text": "先輩、行きましょう。"}],
                "序章", datetime.now(),
            )
            self.assertEqual(total, 1)
            activated = store.activate_preloaded(
                "", "先輩、行きましょう。", "ScriptMessageCommonManager.SetText",
                datetime.now(), "序章", "", "先輩、行きましょう。", 10,
            )
            self.assertEqual(activated, pending[0])
            self.assertEqual(store.rows("序章")[0][3], "マシュ")
            store.close()

    def test_runtime_style_memory_requires_three_distinct_verified_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            ids = []
            for index, japanese in enumerate(
                ["任せるでち。", "急ぐでち。", "料理するでち。"], 1
            ):
                row_id = store.add("紅閻魔", japanese, "live", datetime.now(), "关卡")
                assert row_id
                store.save_translations([{"id": row_id, "zh": f"第{index}句啾。"}], "test")
                ids.append(row_id)
            update = {
                "speaker": "紅閻魔",
                "source_pattern": "でち",
                "zh_rendering": "啾",
            }
            for row_id in ids[:2]:
                store.save_speaker_styles(
                    [{**update, "evidence_line_ids": [row_id]}], {row_id}
                )
            self.assertEqual(
                store.speaker_styles([{"speaker": "紅閻魔", "ja": "そうでち。"}]), []
            )
            store.save_speaker_styles(
                [{**update, "evidence_line_ids": [ids[2]]}], {ids[2]}
            )
            learned = store.speaker_styles(
                [{"speaker": "紅閻魔", "ja": "そうでち。"}]
            )
            self.assertEqual(learned[0]["preferred_zh_rendering"], "啾")
            self.assertEqual(
                store.speaker_styles([{"speaker": "マシュ", "ja": "そうでち。"}]), []
            )
            store.close()

    def test_translation_prompt_uses_context_terms_but_omits_quest_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            first = store.add("マシュ", "先輩。", "live", datetime.now(), "序章")
            second = store.add("マシュ", "カルデアへ行きましょう。", "live", datetime.now(), "序章")
            store.save_translations([{"id": first, "zh": "前辈。"}], "test")
            events = __import__("queue").Queue()
            worker = CodexTranslationWorker(store, RuntimeConfig(), events)
            item = store.translation_items([second])
            prompt_text = worker._build_prompt(item)
            self.assertNotIn('"quest": "序章"', prompt_text)
            self.assertNotIn("序章", prompt_text)
            self.assertIn('"zh": "前辈。"', prompt_text)
            self.assertIn('"ja": "カルデア"', prompt_text)
            self.assertIn('"id": ' + str(second), prompt_text)
            self.assertEqual(format_bilingual_entry("マシュ", "先輩。", "前辈。"), "マシュ 「先輩。」\n译：前辈。")
            store.close()

    def test_beni_enma_dechi_is_locked_to_chirp_not_da(self) -> None:
        line = {
            "speaker": "紅閻魔",
            "ja": "そうなのでち。お願いしまちた。",
        }
        guide = character_style_guide([line])
        self.assertEqual(guide[0]["speaker"], "紅閻魔")
        self.assertIn("不得译成『哒』", guide[0]["required_zh_rendering"])
        self.assertEqual(
            enforce_character_style_translation(
                "紅閻魔", line["ja"], "正是如此哒。已经拜托他了哒。"
            ),
            "正是如此啾。已经拜托他了啾。",
        )
        self.assertEqual(
            enforce_character_style_translation("マシュ", "そうです。", "正是如此哒。"),
            "正是如此哒。",
        )

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            path = Path(temp) / "history.db"
            store = HistoryStore(path)
            row_id = store.add(
                "紅閻魔", line["ja"], "live", datetime.now(), "测试关卡"
            )
            assert row_id
            prompt_text = CodexTranslationWorker(
                store, RuntimeConfig(), __import__("queue").Queue()
            )._build_prompt(store.translation_items([row_id]))
            self.assertIn('"character_style_guide"', prompt_text)
            saved = store.save_translations(
                [{"id": row_id, "zh": "正是如此哒。已经拜托他了哒。"}], "test"
            )
            self.assertEqual(saved[0]["translation"], "正是如此啾。已经拜托他了啾。")
            # Older database rows are repaired conservatively on next start.
            store._db.execute(
                "UPDATE dialogue SET translation = ? WHERE id = ?", ("又变成哒。", row_id)
            )
            store._db.commit()
            store.close()
            reopened = HistoryStore(path)
            self.assertEqual(reopened.rows()[0][6], "又变成啾。")
            reopened.close()

    def test_battle_level_ui_is_stored_but_never_sent_for_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            battle_id = store.add(
                "",
                "聖剣作成 EX  Lv.10",
                "ScriptLineMessage.SetText+ScreenOCRv2",
                datetime.now(),
                "自由关卡（阶段 1）",
            )
            story_id = store.add(
                "マシュ", "行きましょう。", "ScriptBackLog.logData", datetime.now(), "序章"
            )
            assert battle_id and story_id
            battle = store.translation_items([battle_id])[0]
            self.assertEqual(battle["status"], "skipped")
            self.assertEqual(store.untranslated_ids(), [story_id])
            worker = CodexTranslationWorker(store, RuntimeConfig(), __import__("queue").Queue())
            worker.enqueue([battle_id], urgent=True)
            self.assertEqual(worker._take_batch(), [])
            self.assertEqual(
                translation_skip_reason(
                    "", "聖剣作成 EX  Lv.10", "ScriptLineMessage.SetText+ScreenOCRv2"
                ),
                "battle_level_ui",
            )
            # Narration through the same lower-level renderer is retained.
            self.assertEqual(
                translation_skip_reason(
                    "", "静かな夜だった。", "ScriptLineMessage.SetText+ScreenOCRv2"
                ),
                "",
            )
            store.close()

    def test_current_line_preempts_background_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            ids = [
                store.add("A", f"過去{i}", "history", datetime.now(), "同一关卡")
                for i in range(20)
            ]
            current = store.add("B", "現在", "live", datetime.now(), "同一关卡")
            worker = CodexTranslationWorker(
                store, RuntimeConfig(translation_batch_size=40), __import__("queue").Queue()
            )
            worker.enqueue([int(value) for value in ids if value])
            worker.enqueue([current], urgent=True)
            urgent_batch = worker._take_batch()
            self.assertEqual([item["id"] for item in urgent_batch], [current])
            background_batch = worker._take_batch()
            self.assertEqual(len(background_batch), 12)
            store.close()

    def test_newest_visible_line_replaces_older_urgent_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            older = store.add("A", "一つ前", "live", datetime.now(), "同一关卡")
            newest = store.add("B", "現在", "live", datetime.now(), "同一关卡")
            worker = CodexTranslationWorker(
                store, RuntimeConfig(), __import__("queue").Queue()
            )
            worker.enqueue_current(int(older))
            worker.enqueue_current(int(newest))
            self.assertEqual(
                [item["id"] for item in worker._take_batch()], [newest]
            )
            # The superseded line is retained for history and translated later.
            self.assertEqual(
                [item["id"] for item in worker._take_batch()], [older]
            )
            store.close()

    def test_new_current_line_interrupts_an_active_stale_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            older = store.add("A", "前の台詞", "live", datetime.now(), "同一关卡")
            newer = store.add("B", "今の台詞", "live", datetime.now(), "同一关卡")
            worker = CodexTranslationWorker(store, RuntimeConfig(), queue.Queue())
            active_cancel = threading.Event()
            worker._active_ids = {int(older)}
            worker._active_cancel = active_cancel
            worker.enqueue_current(int(newer))
            self.assertTrue(active_cancel.is_set())
            self.assertEqual([item["id"] for item in worker._take_batch()], [newer])
            store.close()

    def test_selected_choice_reuses_option_translation_without_second_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            choice = store.add(
                "【剧情选项】", "1. はい\n2. いいえ", "choice",
                datetime.now(), "选择关卡", display_order=10,
            )
            selected = store.add(
                "【已选择】", "2. いいえ", "selected",
                datetime.now(), "选择关卡", display_order=20,
            )
            self.assertTrue(
                store.link_selected_choice(
                    int(selected), "选择关卡", 1, "いいえ", 20
                )
            )
            self.assertTrue(store.translation_items([int(selected)])[0]["choice_waiting"])
            saved = store.save_translations(
                [{"id": int(choice), "speaker_zh": "", "zh": "1. 是\n2. 否"}],
                "test-model",
            )
            self.assertIn(int(selected), {int(item["id"]) for item in saved})
            selected_item = store.translation_items([int(selected)])[0]
            self.assertEqual(selected_item["translation"], "否")
            self.assertFalse(selected_item["choice_waiting"])
            store.close()

    def test_live_page_is_completed_when_all_preload_fragments_stream_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            fragment_ids, _ = store.add_preloaded(
                [
                    {"speaker": "マシュ", "text": "前半。"},
                    {"speaker": "マシュ", "text": "後半。"},
                ],
                "组合关卡",
                datetime.now(),
            )
            live = store.add(
                "マシュ", "前半。後半。", "live", datetime.now(), "组合关卡"
            )
            self.assertTrue(store.link_preloaded_composition(int(live)))
            store.save_translations(
                [{"id": fragment_ids[0], "speaker_zh": "玛修", "zh": "前半。"}],
                "test-model",
            )
            self.assertEqual(store.translation_items([int(live)])[0]["translation"], "")
            saved = store.save_translations(
                [{"id": fragment_ids[1], "speaker_zh": "玛修", "zh": "后半。"}],
                "test-model",
            )
            self.assertIn(int(live), {int(item["id"]) for item in saved})
            self.assertEqual(
                store.translation_items([int(live)])[0]["translation"],
                "前半。后半。",
            )
            store.close()

    def test_view_counts_are_queried_without_tree_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = HistoryStore(Path(temp) / "history.db")
            store.add("A", "已播放", "live", datetime.now(), "关卡A")
            store.add_preloaded(
                [{"speaker": "B", "text": "预载"}], "关卡A", datetime.now()
            )
            self.assertEqual(store.view_counts("关卡A", False), (1, 0))
            self.assertEqual(store.view_counts("关卡A", True), (2, 1))
            store.close()

    def test_normal_scenario_preload_uses_one_whole_stage_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            pending, total = store.add_preloaded(
                [
                    {"speaker": "マシュ", "text": f"事前翻訳{i}。"}
                    for i in range(191)
                ],
                "批量测试关卡",
                datetime.now(),
            )
            self.assertEqual(total, 191)
            worker = CodexTranslationWorker(
                store,
                RuntimeConfig(translation_batch_size=7),
                __import__("queue").Queue(),
            )
            worker.enqueue(pending)
            first = worker._take_batch()
            second = worker._take_batch()
            self.assertEqual(len(first), 191)
            self.assertEqual(second, [])
            store.close()

    def test_oversized_scenario_falls_back_to_true_one_hundred_row_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            pending, total = store.add_preloaded(
                [
                    {"speaker": "マシュ", "text": f"巨大脚本{i}。"}
                    for i in range(300)
                ],
                "超大批量测试关卡",
                datetime.now(),
            )
            self.assertEqual(total, 300)
            worker = CodexTranslationWorker(
                store, RuntimeConfig(translation_batch_size=7), queue.Queue()
            )
            worker.enqueue(pending)
            self.assertEqual(len(worker._take_batch()), 100)
            self.assertEqual(len(worker._take_batch()), 100)
            self.assertEqual(len(worker._take_batch()), 100)
            store.close()

    def test_long_text_scenario_uses_character_budget_even_below_row_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            pending, _ = store.add_preloaded(
                [
                    {"speaker": "", "text": ("長い場面説明" * 14) + str(i)}
                    for i in range(200)
                ],
                "长文本测试关卡",
                datetime.now(),
            )
            worker = CodexTranslationWorker(store, RuntimeConfig(), queue.Queue())
            worker.enqueue(pending)
            self.assertEqual(len(worker._take_batch()), 100)
            self.assertEqual(len(worker._take_batch()), 100)
            store.close()

    def test_live_and_preload_rows_never_share_a_model_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            live_id = store.add(
                "【剧情选项】", "1. 行く\n2. 戻る", "ScriptSelectDialog.Open",
                datetime.now(), "同一阶段",
            )
            preload_ids, _ = store.add_preloaded(
                [{"speaker": "マシュ", "text": f"事前{i}。"} for i in range(3)],
                "同一阶段", datetime.now(),
            )
            self.assertIsNotNone(live_id)
            worker = CodexTranslationWorker(store, RuntimeConfig(), queue.Queue())
            worker.enqueue([int(live_id), *preload_ids])
            live_batch = worker._take_batch()
            preload_batch = worker._take_batch()
            self.assertTrue(all(not item["preloaded"] for item in live_batch))
            self.assertTrue(all(item["preloaded"] for item in preload_batch))
            self.assertEqual(worker._profile(False)[:2], ("gpt-5.6-terra", "low"))
            self.assertEqual(worker._profile(True)[:2], ("gpt-5.6-sol", "high"))
            store.close()

    def test_preload_uses_its_own_streaming_app_server_profile(self) -> None:
        class PreloadClient:
            def __init__(self):
                self.called = False

            def translate(self, *args, **kwargs):
                self.called = True
                callback = kwargs.get("partial_callback")
                if callback:
                    callback({"id": 1, "speaker_zh": "玛修", "zh": "完成。"})
                return {
                    "translations": [{"id": 1, "speaker_zh": "玛修", "zh": "完成。"}],
                    "memory_updates": [],
                    "style_updates": [],
                }

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            config = RuntimeConfig(
                translation_model="gpt-5.5",
                preload_translation_model="gpt-5.6-sol",
                preload_translation_reasoning="high",
            )
            worker = CodexTranslationWorker(
                store, config, queue.Queue(), scope="preload"
            )
            self.assertEqual(worker._app_server.config.translation_model, "gpt-5.6-sol")
            self.assertEqual(worker._app_server.config.translation_reasoning, "high")
            fake = PreloadClient()
            worker._app_server = fake
            partial: list[dict] = []
            result = worker._invoke(
                "阶段", "prompt", "fallback", is_preload=True,
                partial_callback=partial.append,
            )
            self.assertTrue(fake.called)
            self.assertEqual(result["translations"][0]["zh"], "完成。")
            self.assertEqual(partial[0]["zh"], "完成。")
            store.close()

    def test_live_and_preload_workers_can_run_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            live_id = store.add(
                "【剧情选项】", "1. はい\n2. いいえ", "ScriptSelectDialog.Open",
                datetime.now(), "并行阶段",
            )
            preload_ids, _ = store.add_preloaded(
                [{"speaker": "", "text": "未来の台詞。"}],
                "并行阶段", datetime.now(),
            )
            self.assertIsNotNone(live_id)
            live_worker = CodexTranslationWorker(
                store, RuntimeConfig(), queue.Queue(), scope="live"
            )
            preload_worker = CodexTranslationWorker(
                store, RuntimeConfig(), queue.Queue(), scope="preload"
            )
            live_worker.enqueue([int(live_id), *preload_ids], urgent=True)
            preload_worker.enqueue([int(live_id), *preload_ids])
            self.assertEqual(
                [item["id"] for item in live_worker._take_batch()], [int(live_id)]
            )
            self.assertEqual(
                [item["id"] for item in preload_worker._take_batch()], preload_ids
            )
            store.close()

    def test_explanation_context_contains_the_complete_same_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            store = HistoryStore(Path(temp) / "history.db")
            ids, total = store.add_preloaded(
                [
                    {"speaker": "マシュ", "text": "最初の台詞。"},
                    {"speaker": "", "text": "静かな夜だった。"},
                    {"speaker": "ダンテ", "text": "最後の台詞。"},
                ],
                "説明テスト（段階 2）",
                datetime.now(),
            )
            store.add("別人", "別の関卡。", "live", datetime.now(), "别的关卡")
            payload = store.explanation_context(ids[1])
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(total, 3)
            self.assertEqual(payload["target_id"], ids[1])
            self.assertEqual(len(payload["scenario"]), 3)
            self.assertEqual(
                [item["ja"] for item in payload["scenario"]],
                ["最初の台詞。", "静かな夜だった。", "最後の台詞。"],
            )
            store.close()

    def test_explanation_is_cached_and_structured_for_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            path = Path(temp) / "history.db"
            store = HistoryStore(path)
            row_id = store.add("マシュ", "先輩。", "live", datetime.now(), "测试关卡")
            rendered = render_explanation_result(
                {
                    "meaning": "她在称呼主人公。",
                    "prior_context": "玛修长期以‘前辈’称呼主人公。",
                    "context": "两人正在交谈。",
                    "subtext": "语气亲近。",
                    "notes": ["先輩是玛修惯用的称呼。"],
                    "web_sources": [
                        {
                            "title": "角色资料",
                            "url": "https://example.com/fgo",
                            "summary": "用于确认称呼习惯。",
                        }
                    ],
                    "uncertainty": "",
                }
            )
            self.assertIn("这句话在说什么", rendered)
            self.assertIn("以前发生过什么", rendered)
            self.assertIn("• 先輩是玛修惯用的称呼。", rendered)
            self.assertIn("https://example.com/fgo", rendered)
            store.save_explanation(row_id, rendered, "gpt-test", web_enabled=True)
            store.close()
            reopened = HistoryStore(path)
            cached = reopened.explanation(row_id)
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached["model"], "gpt-test")
            self.assertEqual(cached["explanation"], rendered)
            self.assertTrue(cached["web_enabled"])
            reopened.close()

    def test_explanation_prompt_reads_full_scenario_but_avoids_future_spoilers(self) -> None:
        worker = CodexExplanationWorker(
            object(), RuntimeConfig(), 7, __import__("queue").Queue()
        )
        prompt = worker._prompt(
            {
                "quest": "测试关卡",
                "target_id": 7,
                "target": {"id": 7, "speaker": "マシュ", "ja": "先輩。", "zh": "前辈。"},
                "scenario": [
                    {"id": 6, "speaker": "", "ja": "夜。", "zh": "夜晚。"},
                    {"id": 7, "speaker": "マシュ", "ja": "先輩。", "zh": "前辈。"},
                ],
            }
        )
        self.assertIn("完整 scenario", prompt)
        self.assertIn("不要主动泄露后续事件", prompt)
        self.assertIn("必须先进行有针对性的实时联网搜索", prompt)
        self.assertIn("prior_context", prompt)
        self.assertIn('"target_id": 7', prompt)

    def test_explanation_web_search_is_explicit_and_precedes_exec(self) -> None:
        worker = CodexExplanationWorker(
            object(), RuntimeConfig(codex_path="codex", explanation_web_search=True), 7,
            __import__("queue").Queue(),
        )
        command = worker._command(Path("schema.json"), Path("out.json"), Path("runtime"))
        self.assertIn("--search", command)
        self.assertLess(command.index("--search"), command.index("exec"))
        self.assertIn("read-only", command)

        offline = CodexExplanationWorker(
            object(), RuntimeConfig(codex_path="codex", explanation_web_search=False), 7,
            __import__("queue").Queue(),
        )
        self.assertNotIn(
            "--search",
            offline._command(Path("schema.json"), Path("out.json"), Path("runtime")),
        )


if __name__ == "__main__":
    unittest.main()
