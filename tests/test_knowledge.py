from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fgo_knowledge import (  # noqa: E402
    KnowledgeBase,
    KnowledgeUpdater,
    _atomic_write_json,
    _candidate,
)


class KnowledgeTests(unittest.TestCase):
    def _baseline(self, path: Path) -> Path:
        value = {
            "version": 1,
            "terms": [
                {"ja": "カルデア", "zh": "迦勒底", "scope": "global"},
                {"ja": "マシュ", "zh": "玛修", "scope": "character"},
            ],
        }
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_structured_mooncell_templates_are_parsed_without_prose_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb = KnowledgeBase(root / "runtime", self._baseline(root / "base.json"))
            updater = KnowledgeUpdater(kb, "test")
            page = {
                "title": "玛修·基列莱特",
                "revid": 123,
                "timestamp": "2026-01-01T00:00:00Z",
                "wikitext": """
{{基础数值|中文名=玛修·基列莱特|日文名=マシュ・キリエライト
|中文战斗名=玛修|日文战斗名=マシュ}}
{{宝具|中文名=已然遥远的理想之城|日文名=いまは遙か理想の城
|国服上标=Lord Camelot|日服上标=ロード・キャメロット}}
{{持有技能|加防|荣光坚毅的雪花之壁|誉れ堅き雪花の壁|7|效果}}
普通说明文字不会被当成术语。
""",
            }
            terms = updater._parse_servant_page(page)
            pairs = {(item["ja"], item["zh"], item["kind"]) for item in terms}
            self.assertIn(("マシュ", "玛修", "battle_name"), pairs)
            self.assertIn(("いまは遙か理想の城", "已然遥远的理想之城", "noble_phantasm"), pairs)
            self.assertIn(("誉れ堅き雪花の壁", "荣光坚毅的雪花之壁", "skill"), pairs)
            self.assertFalse(any("普通说明文字" in item["zh"] for item in terms))

    def test_conflict_resolution_prefers_curated_then_wiki_over_atlas_aliases(self) -> None:
        def term(ja: str, zh: str, priority: int, source: str):
            value = _candidate(
                ja,
                zh,
                kind="servant",
                scope="global",
                priority=priority,
                status="verified",
                source_id=source,
                source_url="https://example.invalid",
                source_title=source,
            )
            assert value
            return value

        active, conflicts = KnowledgeUpdater._merge(
            [
                term("メドゥーサ", "歌果", 85, "atlas"),
                term("メドゥーサ", "美杜莎", 92, "mooncell"),
                term("マシュ", "玛修", 100, "bundled"),
                term("マシュ", "玛修酱", 92, "mooncell"),
            ]
        )
        selected = {item["ja"]: item["zh"] for item in active}
        self.assertEqual(selected["メドゥーサ"], "美杜莎")
        self.assertEqual(selected["マシュ"], "玛修")
        self.assertEqual(len(conflicts), 2)

    def test_retrieval_keeps_large_dictionary_out_of_unrelated_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root / "base.json")
            kb = KnowledgeBase(root / "runtime", baseline)
            selected = kb.relevant_terms(
                [{"speaker": "マシュ", "ja": "カルデアに戻りましょう。"}], []
            )
            pairs = {(item["ja"], item["zh"]) for item in selected}
            self.assertIn(("マシュ", "玛修"), pairs)
            self.assertIn(("カルデア", "迦勒底"), pairs)

    def test_speaker_translation_uses_exact_verified_name_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb = KnowledgeBase(root / "runtime", self._baseline(root / "base.json"))
            self.assertEqual(kb.translate_speaker("マシュ"), "玛修")
            self.assertEqual(kb.translate_speaker("マシュー"), "")
            self.assertEqual(kb.translate_speaker(""), "")

    def test_new_curated_speaker_name_overlays_older_runtime_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root / "base.json")
            bundled = json.loads(baseline.read_text(encoding="utf-8"))
            bundled["terms"].append(
                {
                    "ja": "ダンテ",
                    "zh": "但丁",
                    "kind": "character",
                    "scope": "character",
                    "priority": 110,
                    "status": "curated",
                }
            )
            baseline.write_text(json.dumps(bundled, ensure_ascii=False), encoding="utf-8")
            kb = KnowledgeBase(root / "runtime", baseline)
            _atomic_write_json(
                kb.active_path,
                {
                    "knowledge_version": "old-runtime",
                    "terms": [{"ja": "旧", "zh": "旧词"}],
                    "styles": [],
                },
            )
            kb.refresh()
            self.assertEqual(kb.translate_speaker("ダンテ"), "但丁")

    def test_voice_table_learns_only_repeated_character_specific_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb = KnowledgeBase(root / "runtime", self._baseline(root / "base.json"))
            updater = KnowledgeUpdater(kb, "test")
            japanese = [
                "今日は楽しいでち！",
                "一緒に帰るでち。",
                "お客様を待つでちよ！",
                "準備ができたでち。",
                "これは大切でちね。",
                "もう眠いでち。",
                "料理を作るでち！",
                "任せるでちか？",
                "急いで行くでち。",
            ]
            chinese = [
                "今天很开心啾！",
                "一起回去啾。",
                "等待客人啾！",
                "准备好了啾。",
                "这很重要啾。",
                "已经困了啾。",
                "做饭啾！",
                "交给你啾？",
                "快走啾。",
            ]
            rows = "\n".join(
                f"|日文{index}={ja}\n|中文{index}={zh}"
                for index, (ja, zh) in enumerate(zip(japanese, chinese), 1)
            )
            page = {
                "title": "红阎魔/语音",
                "revid": 123,
                "timestamp": "2026-01-01T00:00:00Z",
                "wikitext": "{{#invoke:VoiceTable|main\n" + rows + "\n}}",
            }
            styles = updater._parse_voice_styles(page, "紅閻魔", ["红阎魔"])
            self.assertEqual(
                [(item["ja_pattern"], item["zh_rendering"]) for item in styles],
                [("でち", "啾")],
            )
            # Repeated duplicate rows do not count as independent evidence.
            duplicate = "\n".join(
                f"|日文{index}=これはでち。\n|中文{index}=这是啾。"
                for index in range(1, 10)
            )
            page["wikitext"] = "{{#invoke:VoiceTable|main\n" + duplicate + "\n}}"
            self.assertEqual(updater._parse_voice_styles(page, "紅閻魔", []), [])

    def test_style_retrieval_requires_both_speaker_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root / "base.json")
            value = json.loads(baseline.read_text(encoding="utf-8"))
            value["styles"] = [
                {
                    "speaker": "紅閻魔",
                    "speaker_aliases": ["红阎魔"],
                    "ja_pattern": "でち",
                    "zh_rendering": "啾",
                    "status": "curated",
                }
            ]
            baseline.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            kb = KnowledgeBase(root / "runtime", baseline)
            self.assertEqual(
                kb.relevant_styles([{"speaker": "紅閻魔", "ja": "そうでち。"}])[0][
                    "required_zh_rendering"
                ],
                "啾",
            )
            self.assertEqual(
                kb.relevant_styles([{"speaker": "マシュ", "ja": "そうでち。"}]), []
            )
            self.assertEqual(
                kb.relevant_styles([{"speaker": "紅閻魔", "ja": "そうです。"}]), []
            )

    def test_runtime_versions_can_be_atomically_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kb = KnowledgeBase(root / "runtime", self._baseline(root / "base.json"))
            old = {"knowledge_version": "old", "terms": [{"ja": "旧", "zh": "旧词"}]}
            new = {"knowledge_version": "new", "terms": [{"ja": "新", "zh": "新词"}]}
            _atomic_write_json(kb.previous_path, old)
            _atomic_write_json(kb.active_path, new)
            kb.refresh()
            summary = kb.rollback()
            self.assertEqual(summary["version"], "old")
            self.assertEqual(json.loads(kb.previous_path.read_text(encoding="utf-8"))["knowledge_version"], "new")

    def test_new_curated_styles_overlay_an_older_runtime_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self._baseline(root / "base.json")
            bundled = json.loads(baseline.read_text(encoding="utf-8"))
            bundled["styles"] = [
                {
                    "speaker": "紅閻魔",
                    "ja_pattern": "でち",
                    "zh_rendering": "啾",
                    "priority": 120,
                    "status": "curated",
                }
            ]
            baseline.write_text(json.dumps(bundled, ensure_ascii=False), encoding="utf-8")
            kb = KnowledgeBase(root / "runtime", baseline)
            _atomic_write_json(
                kb.active_path,
                {
                    "knowledge_version": "old-runtime",
                    "terms": [{"ja": "旧", "zh": "旧词"}],
                    "styles": [],
                },
            )
            kb.refresh()
            self.assertEqual(kb.summary()["style_count"], 1)
            self.assertEqual(
                kb.relevant_styles([{"speaker": "紅閻魔", "ja": "そうでち。"}])[0][
                    "required_zh_rendering"
                ],
                "啾",
            )


if __name__ == "__main__":
    unittest.main()
