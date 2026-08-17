from __future__ import annotations

import json
import itertools
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
import unicodedata

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from fgo_knowledge import KnowledgeBase, KnowledgeUpdater, KnowledgeUpdateError

try:
    import frida
except ImportError as exc:  # pragma: no cover - packaged builds include Frida
    raise SystemExit("缺少 Frida Python 模块，无法启动监听器。") from exc


APP_NAME = "FGO 剧情文本监听器"
APP_VERSION = "2.9.5"
PACKAGE = "com.aniplex.fategrandorder"
GADGET_PORT = 27043
SERVER_PORT = 27042
DEFAULT_TRANSLATION_MODEL = "gpt-5.6-terra"
DEFAULT_PRELOAD_TRANSLATION_MODEL = "gpt-5.6-sol"
COMPATIBLE_TRANSLATION_MODEL = "gpt-5.5"
WHOLE_STAGE_MAX_ROWS = 256
WHOLE_STAGE_MAX_JAPANESE_CHARS = 12000


# A restrained, high-contrast visual system inspired by Apple's desktop apps.
# Tk does not provide native material/blur surfaces, so the interface uses
# spacing, typography, flat controls and quiet neutral cards instead of trying
# to imitate translucency with unreliable platform-specific hacks.
UI_BG = "#F5F5F7"
UI_CARD = "#FFFFFF"
UI_TEXT = "#1D1D1F"
UI_SECONDARY = "#6E6E73"
UI_TERTIARY = "#8E8E93"
UI_BORDER = "#D2D2D7"
UI_DIVIDER = "#E5E5EA"
UI_BLUE = "#007AFF"
UI_BLUE_ACTIVE = "#0066D6"
UI_BLUE_TINT = "#EAF3FF"
UI_GREEN = "#34C759"
UI_ORANGE = "#FF9F0A"
UI_RED = "#FF3B30"
UI_JAPANESE = "#3A3A3C"


def apple_button(
    parent: tk.Misc,
    text: str,
    command: Callable[[], None],
    *,
    primary: bool = False,
    destructive: bool = False,
    compact: bool = False,
) -> tk.Button:
    """Create a predictable flat Windows button with Apple-like hierarchy."""
    if primary:
        bg, fg, active = UI_BLUE, "#FFFFFF", UI_BLUE_ACTIVE
    elif destructive:
        bg, fg, active = "#FFF0EF", UI_RED, "#FFE2DF"
    else:
        bg, fg, active = "#ECECF1", UI_TEXT, "#E1E1E7"
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        activebackground=active,
        activeforeground=fg,
        disabledforeground=UI_TERTIARY,
        relief="flat",
        borderwidth=0,
        highlightthickness=0,
        cursor="hand2",
        font=("Microsoft YaHei UI", 9, "bold"),
        padx=12 if compact else 16,
        pady=5 if compact else 7,
    )


# FGO's current Japanese scenario strings use a display substitution table.
# These seed pairs were verified against the game's own backlog.  The resolver
# learns additional pairs from Windows Japanese OCR while the user reads.
SEED_CHARACTER_MAP: dict[str, str] = {
    "母": "ダ", "件": "ン", "氾": "テ",
    "引": "ま", "勻": "っ", "凶": "た", "仁": "く",
    "馴": "攻", "U00028b22": "撃", "互": "が", "趣": "届",
    "井": "か", "卅": "な", "中": "い", "友": "ね", "﹝": "。",
    "今": "さ", "允": "す", "卞": "に", "湮": "大", "五": "き",
    "綃": "違", "亢": "ぎ", "化": "て", "﹜": "、", "煾": "戦",
    "手": "も", "方": "よ", "＃": "…",
}


DISPLAY_TAG_RE = re.compile(r"\[([^\[\]]*)\]")


def strip_display_tags(raw: str) -> str:
    """Turn the scenario string into its visible character sequence."""
    value = (raw or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")

    def replace_tag(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        low = body.lower()
        if low in {"r", "n", "br"}:
            return "\n"
        if body.startswith("#") and ":" in body:
            # FGO ruby syntax: [#方法:かたち] renders 方法 with かたち
            # as the small reading. The base characters are the copyable text.
            return body[1:].split(":", 1)[0]
        if body.startswith("#") and re.search(
            r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]", body[1:]
        ):
            # Some scenario commands wrap a complete visible phrase as
            # [#父親の目が鋭くてな] without a ruby reading.  This payload is
            # dialogue, not a presentation tag.
            return body[1:]
        # NGUI style tags and scenario presentation commands do not represent
        # copied dialogue characters. Unknown tags are retained by
        # normalize_text(), but excluded from OCR alignment here.
        return ""

    return DISPLAY_TAG_RE.sub(replace_tag, value)


def resource_path(name: str) -> Path:
    roots = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle))
    roots.append(Path(__file__).resolve().parent)
    for root in roots:
        candidate = root / name
        if candidate.exists():
            return candidate
    return roots[0] / name


def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "FgoStoryListener"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_knowledge_path() -> Path:
    modern = resource_path("memory/fgo_knowledge_base.json")
    if modern.is_file():
        return modern
    return resource_path("memory/fgo_glossary.json")


def normalize_text(raw: str) -> str:
    """Keep Japanese text intact while removing display-only NGUI tags."""
    text = (raw or "").replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[#([^:\]\[]+):[^\]\[]*\]", r"\1", text)
    text = re.sub(
        r"\[#([^\]\[]*[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff][^\]\[]*)\]",
        r"\1",
        text,
    )
    text = re.sub(r"\[(?:r|n|br)\]", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"\[line\s+\d+\]", "", text, flags=re.IGNORECASE)
    # Ordinary NGUI color/style tags.
    text = re.sub(r"\[(?:-|/?[bius]|[0-9A-Fa-f]{6,8})\]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


BENI_ENMA_SPEAKERS = {"紅閻魔", "红阎魔"}
BENI_ENMA_TIC_MARKERS = ("でち", "まちゅ", "まちぇん", "まちた", "まちょう")


def character_style_guide(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return small, speaker-scoped hard rules that survive glossary rebuilds."""
    for item in lines:
        speaker = sanitize_speaker(str(item.get("speaker", "")))
        japanese = normalize_text(str(item.get("ja", "")))
        if speaker in BENI_ENMA_SPEAKERS and any(
            marker in japanese for marker in BENI_ENMA_TIC_MARKERS
        ):
            return [
                {
                    "speaker": "紅閻魔",
                    "source_markers": list(BENI_ENMA_TIC_MARKERS),
                    "required_zh_rendering": "口齿不清的口癖统一译作句尾『啾』，不得译成『哒』。",
                }
            ]
    return []


def enforce_character_style_translation(speaker: str, japanese: str, translation: str) -> str:
    """Repair only a proven character-specific style regression."""
    result = normalize_text(translation)
    if (
        sanitize_speaker(speaker) in BENI_ENMA_SPEAKERS
        and any(marker in normalize_text(japanese) for marker in BENI_ENMA_TIC_MARKERS)
    ):
        # Mooncell consistently renders Beni-enma's sentence-final dechi tic
        # as 啾. Limit the deterministic repair to sentence-final 哒 so normal
        # lexical content and other speakers are never rewritten.
        result = re.sub(r"哒(?=\s*(?:[。！？!?]|…|$))", "啾", result)
    return result


BATTLE_LEVEL_RE = re.compile(r"(?:^|\s)Lv\.?\s*\d+\s*$", re.IGNORECASE)
NON_STORY_SCREEN_PREFIXES = (
    "この物語はフィクションです",
    "このアプリは無料で遊ぶことができます",
)


def translation_skip_reason(speaker: str, text: str, source: str) -> str:
    """Return a reason only for reliably identified non-scenario UI text."""
    clean_speaker = sanitize_speaker(speaker)
    clean_text = normalize_text(text)
    source_name = str(source or "")
    if (
        not clean_speaker
        and source_name.startswith("ScriptLineMessage.SetText")
        and BATTLE_LEVEL_RE.search(clean_text)
    ):
        return "battle_level_ui"
    if (
        not clean_speaker
        and source_name.startswith("CurrentScreenSnapshot")
        and clean_text.startswith(NON_STORY_SCREEN_PREFIXES)
    ):
        return "system_notice"
    return ""


def compact_ocr_text(value: str) -> str:
    """Remove OCR-inserted spacing while retaining real dialogue symbols."""
    text = (value or "").replace("\u3000", " ")
    text = re.sub(r"\s+", "", text)
    # Common UI/edge artifacts seen next to FGO's name plate.
    text = re.sub(r"(?:-=|=-|--|==|[-=]{2,}|[0〇]$)", "", text)
    return text.strip()


def comparison_text(value: str) -> str:
    """Normalize only for equality checks; never use this as copied output."""
    return unicodedata.normalize("NFKC", compact_ocr_text(value))


def probably_encoded_text(value: str) -> bool:
    """Conservatively identify FGO's substituted scenario character stream.

    A normal Japanese sentence may legitimately contain a seed-map key such
    as ``今``.  Requiring several mapped characters (or a very dense short
    string) prevents a rendered sentence from being decoded a second time.
    """
    chars = [
        ch for ch in strip_display_tags(value)
        if not ch.isspace() and contains_japanese(ch)
    ]
    if not chars:
        return False
    mapped = sum(1 for ch in chars if len(ch) == 1 and ch in SEED_CHARACTER_MAP)
    ratio = mapped / len(chars)
    if len(chars) <= 4:
        return mapped >= 2 and ratio >= 0.60
    return mapped >= 3 and ratio >= 0.30


def managed_text_is_lossless(value: str) -> bool:
    """True only when the managed string can be rendered deterministically."""
    raw = str(value or "")
    if not normalize_text(raw) or probably_encoded_text(raw):
        return False
    for match in DISPLAY_TAG_RE.finditer(raw):
        body = match.group(1).strip()
        low = body.lower()
        if low in {"r", "n", "br", "-", "b", "/b", "i", "/i", "u", "/u", "s", "/s"}:
            continue
        if re.fullmatch(r"line\s+\d+", body, flags=re.IGNORECASE):
            continue
        if re.fullmatch(r"[0-9A-Fa-f]{6,8}", body):
            continue
        if body.startswith("#") and ":" in body and body[1:].split(":", 1)[0]:
            continue
        if body.startswith("#") and re.search(
            r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]", body[1:]
        ):
            continue
        return False
    return True


def backlog_match_key(speaker: str, text: str) -> tuple[str, str]:
    """Occurrence-aware comparison key shared by LOG and live rows."""
    clean_speaker = sanitize_speaker(speaker)
    clean_text = normalize_text(text)
    if clean_speaker == "【已选择】":
        clean_text = re.sub(r"^\d+\.\s*", "", clean_text)
    return clean_speaker, clean_text


def contains_japanese(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]", value or ""))


def sanitize_speaker(value: str) -> str:
    """Remove the verified name-plate artifact without touching 耀星のハサン."""
    text = normalize_text(value)
    return re.sub(r"^耀(?=[\u3040-\u30ff])", "", text)


def quest_label(value: Any) -> str:
    if not isinstance(value, dict):
        return normalize_text(str(value or "")) or "未知关卡"
    master_title = normalize_text(str(value.get("master_title", "")))
    title = normalize_text(str(value.get("title", "")))
    chapter = normalize_text(str(value.get("chapter", "")))
    quest_id = int(value.get("quest_id", 0) or 0)
    phase = int(value.get("phase", 0) or 0)
    script = normalize_text(str(value.get("script", "")))
    if master_title:
        return f"{master_title}（阶段 {phase}）" if phase > 0 else master_title
    if title and title.lower() not in {"dummy", "none", "null"}:
        return f"{title}（阶段 {phase}）" if phase > 0 else title
    if chapter and chapter.lower() not in {"dummy", "none", "null"}:
        return f"{chapter}（阶段 {phase}）" if phase > 0 else chapter
    script_match = re.search(r"(\d+)$", script)
    if quest_id:
        label = f"关卡 {quest_id}"
        if script_match:
            digits = script_match.group(1)
            suffix = digits[len(str(quest_id)) :] if digits.startswith(str(quest_id)) else ""
            if suffix:
                label += f" · 剧情 {suffix}"
        if phase > 0:
            label += f" · 阶段 {phase}"
        return label
    return script or "未知关卡"


def format_entry(speaker: str, text: str) -> str:
    speaker = sanitize_speaker(speaker)
    text = normalize_text(text)
    if speaker.startswith("【") and speaker.endswith("】"):
        return f"{speaker}\n{text}"
    if speaker:
        return f"{speaker} 「{text}」"
    return text


def format_bilingual_entry(speaker: str, text: str, translation: str = "") -> str:
    source = format_entry(speaker, text)
    translated = normalize_text(translation)
    return f"{source}\n译：{translated}" if translated else source


def available_codex_models() -> list[str]:
    """Read Codex' own model cache without changing the user's global config."""
    preferred = [DEFAULT_TRANSLATION_MODEL, "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.4"]
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
    discovered: list[str] = []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        for item in value.get("models", []):
            slug = str(item.get("slug", "")).strip()
            if slug and str(item.get("visibility", "list")) != "hide":
                discovered.append(slug)
    except (OSError, ValueError, TypeError):
        pass
    return list(dict.fromkeys([*preferred, *discovered]))


def codex_fast_models() -> set[str]:
    """Models whose local Codex catalog exposes the priority/Fast tier."""
    path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
    supported: set[str] = set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        for item in value.get("models", []):
            tiers = item.get("service_tiers", [])
            if any(str(tier.get("id", "")) == "priority" for tier in tiers):
                slug = str(item.get("slug", "")).strip()
                if slug:
                    supported.add(slug)
    except (OSError, ValueError, TypeError):
        pass
    return supported or {"gpt-5.5", "gpt-5.4"}


def fast_mode_for(model: str, enabled: bool) -> bool:
    return bool(enabled) and str(model) in codex_fast_models()


def fast_mode_effective(config: "RuntimeConfig", preloaded: bool = False) -> bool:
    if preloaded:
        return fast_mode_for(
            config.preload_translation_model, config.preload_translation_fast_mode
        )
    return fast_mode_for(config.translation_model, config.translation_fast_mode)


def _codex_command(configured: str = "") -> list[str]:
    """Return a shell-free command line for the locally installed Codex CLI."""
    candidate = configured.strip() or shutil.which("codex.cmd") or shutil.which("codex") or ""
    if not candidate:
        common = Path(r"C:\nvm4w\nodejs\codex.cmd")
        candidate = str(common) if common.is_file() else ""
    if not candidate:
        raise RuntimeError("未找到 codex-cli。请先安装并执行 codex login。")
    path = Path(candidate)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        base = path.parent
        node = base / "node.exe"
        script = base / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if node.is_file() and script.is_file():
            return [str(node), str(script)]
        if path.suffix.lower() == ".ps1":
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)]
    return [str(path)]


class HistoryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS dialogue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                quest TEXT NOT NULL DEFAULT '',
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                raw_speaker TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
        columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(dialogue)")}
        if "display_order" not in columns:
            self._db.execute(
                "ALTER TABLE dialogue ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0"
            )
        if "quest" not in columns:
            self._db.execute(
                "ALTER TABLE dialogue ADD COLUMN quest TEXT NOT NULL DEFAULT ''"
            )
        translation_columns = {
            "translation": "TEXT NOT NULL DEFAULT ''",
            "translation_model": "TEXT NOT NULL DEFAULT ''",
            "translation_status": "TEXT NOT NULL DEFAULT ''",
            "translation_updated_at": "TEXT NOT NULL DEFAULT ''",
            "preloaded": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in translation_columns.items():
            if name not in columns:
                self._db.execute(f"ALTER TABLE dialogue ADD COLUMN {name} {definition}")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS quest_memory (
                quest TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (quest, memory_key)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS speaker_style_memory (
                speaker TEXT NOT NULL,
                source_pattern TEXT NOT NULL,
                zh_rendering TEXT NOT NULL,
                evidence_ids TEXT NOT NULL DEFAULT '',
                evidence_texts TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (speaker, source_pattern, zh_rendering)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS speaker_name_translation (
                quest TEXT NOT NULL,
                speaker TEXT NOT NULL,
                translation TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (quest, speaker)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS dialogue_explanation (
                row_id INTEGER PRIMARY KEY,
                explanation TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                web_enabled INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS choice_translation_link (
                selected_row_id INTEGER PRIMARY KEY,
                choice_row_id INTEGER NOT NULL,
                choice_index INTEGER NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS choice_translation_link_source "
            "ON choice_translation_link(choice_row_id)"
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS preload_composition_link (
                live_row_id INTEGER NOT NULL,
                fragment_row_id INTEGER NOT NULL,
                fragment_order INTEGER NOT NULL,
                PRIMARY KEY (live_row_id, fragment_order)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS preload_composition_link_fragment "
            "ON preload_composition_link(fragment_row_id)"
        )
        self._db.execute(
            "DELETE FROM preload_composition_link WHERE live_row_id NOT IN "
            "(SELECT id FROM dialogue) OR fragment_row_id NOT IN "
            "(SELECT id FROM dialogue)"
        )
        explanation_columns = {
            str(row[1]) for row in self._db.execute("PRAGMA table_info(dialogue_explanation)")
        }
        if "web_enabled" not in explanation_columns:
            self._db.execute(
                "ALTER TABLE dialogue_explanation "
                "ADD COLUMN web_enabled INTEGER NOT NULL DEFAULT 0"
            )
        self._db.execute(
            "UPDATE dialogue SET display_order = id * 10 WHERE display_order = 0"
        )
        self._db.commit()
        backup_path = path.with_name("history-before-v1.2.db")
        row_count = int(self._db.execute("SELECT COUNT(*) FROM dialogue").fetchone()[0])
        if row_count and not backup_path.exists():
            backup = sqlite3.connect(backup_path)
            try:
                self._db.backup(backup)
            finally:
                backup.close()
        v2_backup = path.with_name("history-before-v2.0.db")
        if row_count and not v2_backup.exists():
            backup = sqlite3.connect(v2_backup)
            try:
                self._db.backup(backup)
            finally:
                backup.close()
        v251_backup = path.with_name("history-before-v2.5.1.db")
        if row_count and not v251_backup.exists():
            backup = sqlite3.connect(v251_backup)
            try:
                self._db.backup(backup)
            finally:
                backup.close()
        v265_backup = path.with_name("history-before-v2.6.5.db")
        if row_count and not v265_backup.exists():
            backup = sqlite3.connect(v265_backup)
            try:
                self._db.backup(backup)
            finally:
                backup.close()
        v269_backup = path.with_name("history-before-v2.6.9.db")
        if row_count and not v269_backup.exists():
            backup = sqlite3.connect(v269_backup)
            try:
                self._db.backup(backup)
            finally:
                backup.close()
        # Older builds OCR'ed the backlog renderer asynchronously. Those rows
        # contain control markers and mismatched pages; the exact game LOG is
        # now re-imported directly from ScriptBackLog.logData.
        self._db.execute(
            "DELETE FROM dialogue WHERE source LIKE 'ScriptLogMessage.%'"
        )
        self._db.execute(
            "DELETE FROM dialogue WHERE source = "
            "'ScriptMessageCommonManager.AddText+ScreenOCR'"
        )
        self._db.execute(
            "DELETE FROM dialogue WHERE source = 'CurrentScreenSnapshot' "
            "AND (text LIKE '%FGOPROJECT%' OR text IN "
            "('旧:ここをタッチで表示', 'ここをタッチで表示'))"
        )
        # v2.6.3-v2.6.4 treated every executeDataList item inside a message
        # block as prose. That staged animation parameters such as "I 0.1"
        # and "time 2.0" for translation. They were never visible history;
        # discard only those hidden preload rows. The new grouped source is
        # ScriptManager.MessageGroups.
        self._db.execute(
            "DELETE FROM dialogue WHERE preloaded = 1 "
            "AND source LIKE 'ScriptManager.executeDataList%'"
        )
        # Combat skill labels and fixed title-screen notices are useful as
        # diagnostics, but are not scenario dialogue. Keep them in history
        # while permanently excluding them from AI queues and context.
        for row_id, speaker, text, source in self._db.execute(
            "SELECT id, speaker, text, source FROM dialogue"
        ).fetchall():
            if translation_skip_reason(str(speaker), str(text), str(source)):
                self._db.execute(
                    "UPDATE dialogue SET translation_status = 'skipped' WHERE id = ?",
                    (row_id,),
                )
        # v1.1 could apply the encoded-character table to a string that was
        # already rendered Japanese.  Repair only high-similarity, low-map-
        # density rows; genuinely encoded historical strings are untouched.
        for row_id, speaker, text, raw_speaker, raw_text, source in self._db.execute(
            "SELECT id, speaker, text, raw_speaker, raw_text, source FROM dialogue "
            "WHERE source LIKE '%ScreenOCRv2'"
        ).fetchall():
            visible_text = normalize_text(strip_display_tags(str(raw_text)))
            old_text_cmp = comparison_text(str(text))
            raw_text_cmp = comparison_text(visible_text)
            if (
                visible_text
                and not probably_encoded_text(visible_text)
                and old_text_cmp
                and SequenceMatcher(None, old_text_cmp, raw_text_cmp).ratio() >= 0.72
            ):
                visible_speaker = sanitize_speaker(strip_display_tags(str(raw_speaker)))
                self._db.execute(
                    "UPDATE dialogue SET speaker = ?, text = ? WHERE id = ?",
                    (visible_speaker or sanitize_speaker(str(speaker)), visible_text, row_id),
                )
        for row_id, speaker in self._db.execute(
            "SELECT id, speaker FROM dialogue WHERE speaker LIKE '耀%'"
        ).fetchall():
            clean = sanitize_speaker(str(speaker))
            if clean != speaker:
                self._db.execute("UPDATE dialogue SET speaker = ? WHERE id = ?", (clean, row_id))
        # v2.6.8 joined every parsed fragment with a newline whenever the
        # rendered LOG page contained any line break. Those were script-token
        # boundaries, not visual paragraph boundaries, and produced the tall,
        # narrow-looking Chinese layout. Only repair rows proven to have reused
        # an older preload translation (translation time predates display).
        self._db.execute(
            "UPDATE dialogue SET translation = "
            "replace(replace(translation, char(13) || char(10), ''), char(10), '') "
            "WHERE preloaded = 0 AND source = 'ScriptBackLog.logData' "
            "AND translation <> '' AND translation_updated_at <> '' "
            "AND translation_updated_at < captured_at AND translation LIKE '%' || char(10) || '%'"
        )
        # A direct managed-text event and the authoritative in-game LOG event
        # can cross in the Python queue (notably when an older build waited for
        # OCR).  Reconcile exact same-page pairs regardless of display_order so
        # the late direct event cannot create a duplicate after LOG insertion.
        backlog_rows: dict[tuple[str, str, str], list[tuple[int, datetime]]] = defaultdict(list)
        for row_id, captured, quest, speaker, text in self._db.execute(
            "SELECT id, captured_at, quest, speaker, text FROM dialogue "
            "WHERE preloaded = 0 AND source = 'ScriptBackLog.logData'"
        ).fetchall():
            try:
                when = datetime.fromisoformat(str(captured))
            except ValueError:
                continue
            backlog_rows[(str(quest), str(speaker), str(text))].append((int(row_id), when))
        for (
            row_id, captured, quest, speaker, text, translation,
            model, status, updated,
        ) in self._db.execute(
            "SELECT id, captured_at, quest, speaker, text, translation, "
            "translation_model, translation_status, translation_updated_at FROM dialogue "
            "WHERE preloaded = 0 AND source <> 'ScriptBackLog.logData'"
        ).fetchall():
            try:
                when = datetime.fromisoformat(str(captured))
            except ValueError:
                continue
            matches = backlog_rows.get((str(quest), str(speaker), str(text)), [])
            if not matches:
                continue
            log_id, distance = min(
                ((candidate_id, abs((when - candidate_time).total_seconds()))
                 for candidate_id, candidate_time in matches),
                key=lambda item: item[1],
            )
            if distance >= 3:
                continue
            if str(translation):
                self._db.execute(
                    "UPDATE dialogue SET translation = CASE WHEN translation = '' THEN ? ELSE translation END, "
                    "translation_model = CASE WHEN translation = '' THEN ? ELSE translation_model END, "
                    "translation_status = CASE WHEN translation = '' THEN ? ELSE translation_status END, "
                    "translation_updated_at = CASE WHEN translation = '' THEN ? ELSE translation_updated_at END "
                    "WHERE id = ?",
                    (str(translation), str(model), str(status), str(updated), log_id),
                )
            self._db.execute("DELETE FROM dialogue WHERE id = ?", (int(row_id),))
        # Repair the known Beni-enma style regression produced by earlier AI
        # sessions. This migration is deliberately speaker-, source-marker-
        # and sentence-ending-specific rather than a global text replacement.
        for row_id, speaker, japanese, translation in self._db.execute(
            "SELECT id, speaker, text, translation FROM dialogue WHERE translation <> ''"
        ).fetchall():
            corrected = enforce_character_style_translation(
                str(speaker), str(japanese), str(translation)
            )
            if corrected != str(translation):
                self._db.execute(
                    "UPDATE dialogue SET translation = ? WHERE id = ?",
                    (corrected, row_id),
                )
        self._db.commit()

    def add(
        self,
        speaker: str,
        text: str,
        source: str,
        captured_at: datetime,
        quest: str = "未知关卡",
        raw_speaker: str | None = None,
        raw_text: str | None = None,
        display_order: int | None = None,
    ) -> int | None:
        clean_speaker = sanitize_speaker(speaker)
        clean_text = normalize_text(text)
        clean_quest = normalize_text(quest) or "未知关卡"
        if not clean_text or clean_text.strip().lower() == "select":
            return None
        with self._lock:
            previous = self._db.execute(
                "SELECT id, captured_at, speaker, raw_speaker, source FROM dialogue "
                "WHERE preloaded = 0 "
                "AND quest = ? AND speaker = ? AND text = ? "
                "ORDER BY id DESC LIMIT 1",
                (clean_quest, clean_speaker, clean_text),
            ).fetchone()
            if previous:
                try:
                    old = datetime.fromisoformat(previous[1])
                    if abs((captured_at - old).total_seconds()) < 3:
                        return None
                except ValueError:
                    pass
            # A live SetText callback can arrive before SetTalkName while the
            # game's own backlog already contains the authoritative speaker.
            # Reconcile only an otherwise identical line inside the narrow
            # event window, and only when exactly one side has no speaker.
            # This cannot merge two real speakers saying the same short line.
            speaker_candidate = self._db.execute(
                "SELECT id, captured_at, speaker, raw_speaker, source FROM dialogue "
                "WHERE preloaded = 0 AND quest = ? AND text = ? "
                "AND ((speaker = '' AND ? <> '') OR (speaker <> '' AND ? = '')) "
                "ORDER BY id DESC LIMIT 1",
                (clean_quest, clean_text, clean_speaker, clean_speaker),
            ).fetchone()
            if speaker_candidate:
                try:
                    old = datetime.fromisoformat(str(speaker_candidate[1]))
                    close_enough = abs((captured_at - old).total_seconds()) < 3
                except ValueError:
                    close_enough = False
                if close_enough:
                    row_id = int(speaker_candidate[0])
                    if clean_speaker and not str(speaker_candidate[2]):
                        # An empty speaker in ScriptBackLog is meaningful: FGO
                        # uses it for narration, scene descriptions and inner
                        # thoughts. A late/stale SetTalkName callback must not
                        # turn such a record into spoken dialogue.
                        if str(speaker_candidate[4]).startswith("ScriptBackLog"):
                            return None
                        self._db.execute(
                            "UPDATE dialogue SET speaker = ?, raw_speaker = ?, source = ? "
                            "WHERE id = ?",
                            (
                                clean_speaker,
                                speaker if raw_speaker is None else raw_speaker,
                                source or str(speaker_candidate[4]),
                                row_id,
                            ),
                        )
                        self._db.commit()
                        return row_id
                    return None
            if display_order is None:
                value = self._db.execute(
                    "SELECT COALESCE(MAX(display_order), 0) + 10 FROM dialogue"
                ).fetchone()
                display_order = int(value[0])
            translation_status = (
                "skipped" if translation_skip_reason(clean_speaker, clean_text, source) else ""
            )
            cur = self._db.execute(
                """
                INSERT INTO dialogue
                (captured_at, display_order, quest, speaker, text,
                 raw_speaker, raw_text, source, translation_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    captured_at.isoformat(timespec="milliseconds"),
                    display_order,
                    clean_quest,
                    clean_speaker,
                    clean_text,
                    speaker if raw_speaker is None else raw_speaker,
                    text if raw_text is None else raw_text,
                    source or "",
                    translation_status,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def max_order(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(display_order), 0) FROM dialogue WHERE preloaded = 0"
            ).fetchone()
            return int(row[0])

    def add_preloaded(
        self,
        entries: list[dict[str, Any]],
        quest: str,
        captured_at: datetime,
        source: str = "ScriptManager.AnalysScript+Preload",
    ) -> tuple[list[int], int]:
        """Stage a whole scenario invisibly while retaining reusable translations."""
        clean: list[tuple[str, str]] = []
        for entry in entries[:4096]:
            speaker = sanitize_speaker(str(entry.get("speaker", "")))
            text = normalize_text(str(entry.get("text", "")))
            if (
                text
                and text.strip().lower() != "select"
                and not translation_skip_reason(speaker, text, source)
            ):
                clean.append((speaker, text))
        if not clean:
            return [], 0
        clean_quest = normalize_text(quest) or "未知关卡"
        with self._lock:
            cached: dict[tuple[str, str], tuple[str, str, str, str]] = {}
            text_only: dict[str, tuple[str, str, str, str]] = {}
            for speaker, text, translation, model, status, updated in self._db.execute(
                "SELECT speaker, text, translation, translation_model, "
                "translation_status, translation_updated_at FROM dialogue "
                "WHERE quest = ? AND translation <> '' ORDER BY preloaded, id",
                (clean_quest,),
            ).fetchall():
                value = (str(translation), str(model), str(status), str(updated))
                cached.setdefault((str(speaker), str(text)), value)
                text_only.setdefault(str(text), value)
            self._db.execute(
                "DELETE FROM dialogue WHERE quest = ? AND preloaded = 1", (clean_quest,)
            )
            self._db.execute(
                "DELETE FROM preload_composition_link WHERE fragment_row_id NOT IN "
                "(SELECT id FROM dialogue)"
            )
            base = int(
                self._db.execute(
                    "SELECT COALESCE(MAX(display_order), 0) + 2000000 FROM dialogue "
                    "WHERE preloaded = 0"
                ).fetchone()[0]
            )
            pending: list[int] = []
            for index, (speaker, text) in enumerate(clean):
                old = cached.get((speaker, text))
                if old is None and not speaker:
                    old = text_only.get(text)
                old = old or ("", "", "", "")
                translation, model, status, updated = old
                if translation:
                    status = "done"
                cur = self._db.execute(
                    """
                    INSERT INTO dialogue
                    (captured_at, display_order, quest, speaker, text,
                     raw_speaker, raw_text, source, translation,
                     translation_model, translation_status,
                     translation_updated_at, preloaded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        captured_at.isoformat(timespec="milliseconds"),
                        base + index * 10,
                        clean_quest,
                        speaker,
                        text,
                        speaker,
                        text,
                        source,
                        translation,
                        model,
                        status,
                        updated,
                    ),
                )
                row_id = int(cur.lastrowid)
                if not translation:
                    pending.append(row_id)
            self._db.commit()
            return pending, len(clean)

    def _find_preloaded_fragment_window_locked(
        self, quest: str, text: str
    ) -> list[tuple[Any, ...]] | None:
        candidates = list(
            self._db.execute(
                "SELECT id, speaker, text, translation, translation_model, "
                "translation_status, translation_updated_at FROM dialogue "
                "WHERE preloaded = 1 AND quest = ? ORDER BY display_order, id",
                (quest,),
            )
        )
        target = comparison_text(text)
        if not target:
            return None
        for start in range(len(candidates)):
            combined = ""
            window: list[tuple[Any, ...]] = []
            for candidate in candidates[start : start + 32]:
                window.append(candidate)
                combined += comparison_text(str(candidate[2]))
                if combined == target and len(window) >= 2:
                    return window
                if len(combined) >= len(target):
                    break
        return None

    def link_preloaded_composition(self, live_row_id: int) -> bool:
        with self._lock:
            live = self._db.execute(
                "SELECT quest, text, translation FROM dialogue "
                "WHERE id = ? AND preloaded = 0",
                (int(live_row_id),),
            ).fetchone()
            if not live or str(live[2]):
                return False
            window = self._find_preloaded_fragment_window_locked(
                str(live[0]), str(live[1])
            )
            if not window:
                return False
            if all(str(item[3]) for item in window):
                updated = max(str(item[6]) for item in window)
                model = next((str(item[4]) for item in window if str(item[4])), "")
                translation = "".join(str(item[3]) for item in window)
                self._db.execute(
                    "UPDATE dialogue SET translation = ?, translation_model = ?, "
                    "translation_status = 'done', translation_updated_at = ? "
                    "WHERE id = ? AND translation = ''",
                    (translation, model, updated, int(live_row_id)),
                )
                self._db.commit()
                return True
            self._db.execute(
                "DELETE FROM preload_composition_link WHERE live_row_id = ?",
                (int(live_row_id),),
            )
            self._db.executemany(
                "INSERT INTO preload_composition_link "
                "(live_row_id, fragment_row_id, fragment_order) VALUES (?, ?, ?)",
                [
                    (int(live_row_id), int(item[0]), index)
                    for index, item in enumerate(window)
                ],
            )
            self._db.commit()
            return True

    def activate_preloaded(
        self,
        speaker: str,
        text: str,
        source: str,
        captured_at: datetime,
        quest: str,
        raw_speaker: str,
        raw_text: str,
        display_order: int,
    ) -> int | None:
        clean_speaker = sanitize_speaker(speaker)
        clean_text = normalize_text(text)
        clean_quest = normalize_text(quest) or "未知关卡"
        if not clean_text:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM dialogue WHERE preloaded = 1 AND quest = ? AND text = ? "
                "ORDER BY CASE WHEN speaker = ? THEN 0 WHEN speaker = '' THEN 1 ELSE 2 END, "
                "display_order, id LIMIT 1",
                (clean_quest, clean_text, clean_speaker),
            ).fetchone()
            if not row:
                # The parsed script stores visible screen lines separately,
                # while ScriptBackLog can return a complete two-line page.
                # Reuse a short consecutive fragment window only after every
                # fragment already has a translation. Character equality is
                # exact after whitespace normalization; no fuzzy guessing.
                merged = self._find_preloaded_fragment_window_locked(
                    clean_quest, clean_text
                )
                if merged is None or not all(str(item[3]) for item in merged):
                    return None
                row_id = int(merged[0][0])
                # Parsed fragments do not correspond one-to-one with visual
                # line breaks. Concatenate the already contextual translations
                # and let the overlay/LOG wrap at its actual window width.
                merged_translation = "".join(str(item[3]) for item in merged)
                merged_model = next(
                    (str(item[4]) for item in merged if str(item[4])), ""
                )
                merged_updated = max(str(item[6]) for item in merged)
                self._db.execute(
                    "UPDATE dialogue SET text = ?, translation = ?, translation_model = ?, "
                    "translation_status = 'done', translation_updated_at = ? WHERE id = ?",
                    (
                        clean_text,
                        merged_translation,
                        merged_model,
                        merged_updated,
                        row_id,
                    ),
                )
                self._db.executemany(
                    "DELETE FROM dialogue WHERE id = ?",
                    [(int(item[0]),) for item in merged[1:]],
                )
                self._db.execute(
                    "DELETE FROM preload_composition_link WHERE fragment_row_id NOT IN "
                    "(SELECT id FROM dialogue)"
                )
                row = (row_id,)
            row_id = int(row[0])
            self._db.execute(
                "UPDATE dialogue SET preloaded = 0, captured_at = ?, display_order = ?, "
                "speaker = CASE WHEN ? <> '' THEN ? ELSE speaker END, "
                "raw_speaker = CASE WHEN ? <> '' THEN ? ELSE raw_speaker END, "
                "raw_text = ?, source = ? WHERE id = ?",
                (
                    captured_at.isoformat(timespec="milliseconds"),
                    display_order,
                    clean_speaker,
                    clean_speaker,
                    clean_speaker,
                    raw_speaker,
                    raw_text,
                    source,
                    row_id,
                ),
            )
            self._db.commit()
            return row_id

    def add_backlog(
        self,
        entries: list[dict[str, Any]],
        quest: str,
        captured_at: datetime,
        before_order: int,
        source: str = "ScriptBackLog.logData",
    ) -> list[int]:
        clean: list[tuple[str, str]] = []
        for entry in entries:
            speaker = sanitize_speaker(str(entry.get("speaker", "")))
            text = normalize_text(str(entry.get("text", "")))
            if text and text.strip().lower() != "select":
                clean.append((speaker, text))
        if not clean:
            return []
        clean_quest = normalize_text(quest) or "未知关卡"
        with self._lock:
            # Match the complete game LOG against rows already captured live.
            # LCS rather than a set is intentional: repeated lines such as
            # "……" keep their correct occurrence count and story order.
            wanted = [backlog_match_key(speaker, text) for speaker, text in clean]
            n = len(wanted)

            def compatible(
                wanted_key: tuple[str, str], existing_key: tuple[str, str]
            ) -> bool:
                wanted_speaker, wanted_text = wanted_key
                existing_speaker, existing_text = existing_key
                return wanted_text == existing_text and (
                    wanted_speaker == existing_speaker
                    or not wanted_speaker
                    or not existing_speaker
                )

            def visible_rows() -> list[tuple[Any, ...]]:
                return list(
                    self._db.execute(
                        "SELECT id, display_order, quest, speaker, text FROM dialogue "
                        "WHERE preloaded = 0 ORDER BY display_order, id"
                    )
                )

            def lcs_matches(global_rows: list[tuple[Any, ...]]) -> dict[int, int]:
                eligible: list[tuple[int, tuple[str, str]]] = []
                for global_index, row in enumerate(global_rows):
                    if str(row[2]) != clean_quest or str(row[3]) == "【剧情选项】":
                        continue
                    eligible.append(
                        (global_index, backlog_match_key(str(row[3]), str(row[4])))
                    )
                wanted_count, m = len(wanted), len(eligible)
                # Normal gameplay is append-only: after each AddLog, the new
                # game LOG and locally visible rows are either identical or
                # the local side contains only the newly rendered tail.  This
                # O(n) path avoids allocating an O(n*m) Python matrix on every
                # click.  Exact order and occurrence count are still checked,
                # so ambiguous/non-append cases conservatively fall through
                # to the original LCS reconciliation.
                if m <= wanted_count:
                    if all(
                        compatible(wanted[index], eligible[index][1])
                        for index in range(m)
                    ):
                        return {
                            index: eligible[index][0] for index in range(m)
                        }
                    offset = wanted_count - m
                    if all(
                        compatible(wanted[offset + index], eligible[index][1])
                        for index in range(m)
                    ):
                        return {
                            offset + index: eligible[index][0]
                            for index in range(m)
                        }
                dp = [[0] * (m + 1) for _ in range(wanted_count + 1)]
                for i in range(wanted_count - 1, -1, -1):
                    for j in range(m - 1, -1, -1):
                        if compatible(wanted[i], eligible[j][1]):
                            dp[i][j] = dp[i + 1][j + 1] + 1
                        else:
                            dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
                result: dict[int, int] = {}
                i = j = 0
                while i < wanted_count and j < m:
                    if (
                        compatible(wanted[i], eligible[j][1])
                        and dp[i][j] == dp[i + 1][j + 1] + 1
                    ):
                        result[i] = eligible[j][0]
                        i += 1
                        j += 1
                    elif dp[i + 1][j] >= dp[i][j + 1]:
                        i += 1
                    else:
                        j += 1
                return result

            global_rows = visible_rows()
            matched = lcs_matches(global_rows)

            # A completed scenario preload is deliberately hidden. When the
            # authoritative game LOG exposes a page, activate that exact cache
            # row (or an exact consecutive fragment sequence) before creating
            # a new untranslated row. Previously only SetText used this path,
            # so every LOG page was unnecessarily sent to AI a second time.
            activated_pending: list[int] = []
            activated_any = False
            for index in (value for value in range(len(wanted)) if value not in matched):
                speaker, text = clean[index]
                row_id = self.activate_preloaded(
                    speaker,
                    text,
                    source,
                    captured_at,
                    clean_quest,
                    speaker,
                    text,
                    before_order + index,
                )
                if row_id is None:
                    continue
                activated_any = True
                translated = self._db.execute(
                    "SELECT translation FROM dialogue WHERE id = ?", (row_id,)
                ).fetchone()
                if translated and not str(translated[0]):
                    activated_pending.append(row_id)
            if activated_any:
                global_rows = visible_rows()
                matched = lcs_matches(global_rows)

            # ScriptBackLog is the authoritative rendered LOG. Its empty
            # speaker is also meaningful (narration, scene description or an
            # internal thought), so reconcile in both directions: fill a name
            # that arrived late, or clear a stale live name for a nameless LOG
            # entry. Never manufacture a "旁白" speaker.
            speaker_repairs: list[tuple[str, str, str, int]] = []
            for wanted_index, global_index in matched.items():
                wanted_speaker, _wanted_text = wanted[wanted_index]
                row = global_rows[global_index]
                existing_speaker = sanitize_speaker(str(row[3]))
                if wanted_speaker != existing_speaker:
                    speaker_repairs.append(
                        (wanted_speaker, wanted_speaker, source, int(row[0]))
                    )
            if speaker_repairs:
                self._db.executemany(
                    "UPDATE dialogue SET speaker = ?, raw_speaker = ?, source = ? WHERE id = ?",
                    speaker_repairs,
                )

            missing = [index for index in range(n) if index not in matched]
            if not missing:
                if speaker_repairs:
                    self._db.commit()
                return activated_pending

            # Place each missing run immediately before its next matched LOG
            # row, or after its previous match. With no match, use the session
            # anchor so the imported LOG precedes new live rows.
            insert_at: dict[int, list[int]] = defaultdict(list)
            for index in missing:
                next_matches = [
                    (wanted_index, global_index)
                    for wanted_index, global_index in matched.items()
                    if wanted_index > index
                ]
                if next_matches:
                    position = min(next_matches)[1]
                else:
                    previous = [
                        (wanted_index, global_index)
                        for wanted_index, global_index in matched.items()
                        if wanted_index < index
                    ]
                    if previous:
                        position = max(previous)[1] + 1
                    else:
                        # A listener session can cross quest phases.  Earlier
                        # live rows from the previous phase may still carry
                        # the session's large temporary order, so comparing
                        # against before_order would insert the new phase in
                        # front of the previous phase's ending.  With no LOG
                        # match, place the imported prefix before the first
                        # live row of this exact quest, or append it if this is
                        # the first row for the quest.
                        position = next(
                            (
                                row_index for row_index, row in enumerate(global_rows)
                                if str(row[2]) == clean_quest
                            ),
                            len(global_rows),
                        )
                insert_at[position].append(index)

            ids: list[int] = list(activated_pending)
            inserted_ids: dict[int, int] = {}
            for index in missing:
                speaker, text = clean[index]
                cur = self._db.execute(
                    """
                    INSERT INTO dialogue
                    (captured_at, display_order, quest, speaker, text,
                     raw_speaker, raw_text, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        captured_at.isoformat(timespec="milliseconds"),
                        -1,
                        clean_quest,
                        speaker,
                        text,
                        speaker,
                        text,
                        source,
                    ),
                )
                row_id = int(cur.lastrowid)
                inserted_ids[index] = row_id
                ids.append(row_id)
                self.link_preloaded_composition(row_id)

            # Re-number in the reconciled order. The database is small and
            # this avoids fragile assumptions about integer gaps after several
            # incremental LOG snapshots.
            final_ids: list[int] = []
            for position in range(len(global_rows) + 1):
                final_ids.extend(inserted_ids[index] for index in insert_at.get(position, []))
                if position < len(global_rows):
                    final_ids.append(int(global_rows[position][0]))
            self._db.executemany(
                "UPDATE dialogue SET display_order = ? WHERE id = ?",
                [(order * 10, row_id) for order, row_id in enumerate(final_ids, 1)],
            )
            self._db.commit()
            return ids

    def latest_visible_id(self, quest: str, speaker: str, text: str) -> int | None:
        """Locate the authoritative visible occurrence of a LOG page."""
        clean_quest = normalize_text(quest) or "未知关卡"
        clean_speaker = sanitize_speaker(speaker)
        clean_text = normalize_text(text)
        if not clean_text:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT id FROM dialogue WHERE preloaded = 0 AND quest = ? AND text = ? "
                "AND (speaker = ? OR speaker = '' OR ? = '') "
                "ORDER BY display_order DESC, id DESC LIMIT 1",
                (clean_quest, clean_text, clean_speaker, clean_speaker),
            ).fetchone()
            return int(row[0]) if row else None

    def rows(self, quest: str | None = None) -> list[tuple[Any, ...]]:
        with self._lock:
            where = " WHERE preloaded = 0 AND quest = ?" if quest else " WHERE preloaded = 0"
            params = (quest,) if quest else ()
            return list(
                self._db.execute(
                    "SELECT id, captured_at, quest, speaker, text, source, "
                    "translation, translation_model, translation_status, translation_updated_at "
                    f"FROM dialogue{where} ORDER BY display_order, id",
                    params,
                )
            )

    def view_rows(
        self, quest: str | None = None, include_preloaded: bool = False
    ) -> list[tuple[Any, ...]]:
        """Rows for the database table, with an explicit preload-state field."""
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if not include_preloaded:
                clauses.append("preloaded = 0")
            if quest:
                clauses.append("quest = ?")
                params.append(quest)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            return list(
                self._db.execute(
                    "SELECT id, captured_at, quest, speaker, text, source, "
                    "translation, translation_model, translation_status, "
                    f"translation_updated_at, preloaded FROM dialogue{where} "
                    "ORDER BY display_order, id",
                    tuple(params),
                )
            )

    def translation_items(self, row_ids: list[int]) -> list[dict[str, Any]]:
        if not row_ids:
            return []
        placeholders = ",".join("?" for _ in row_ids)
        with self._lock:
            rows = self._db.execute(
                "SELECT d.id, d.display_order, d.quest, d.speaker, d.text, "
                "d.translation, d.translation_status, d.preloaded, "
                "EXISTS(SELECT 1 FROM choice_translation_link link "
                "JOIN dialogue source ON source.id = link.choice_row_id "
                "WHERE link.selected_row_id = d.id AND source.translation = '' "
                "AND source.translation_status <> 'error') FROM dialogue d "
                f"WHERE d.id IN ({placeholders}) ORDER BY d.display_order, d.id",
                tuple(row_ids),
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "display_order": int(row[1]),
                "quest": str(row[2]),
                "speaker": str(row[3]),
                "text": str(row[4]),
                "translation": str(row[5]),
                "status": str(row[6]),
                "preloaded": bool(row[7]),
                "choice_waiting": bool(row[8]),
            }
            for row in rows
        ]

    def untranslated_ids(self, limit: int = 10000) -> list[int]:
        with self._lock:
            return [
                int(row[0])
                for row in self._db.execute(
                    "SELECT d.id FROM dialogue d WHERE d.translation = '' "
                    "AND d.preloaded = 0 "
                    "AND d.translation_status NOT IN ('translating', 'skipped') "
                    "AND NOT EXISTS(SELECT 1 FROM choice_translation_link link "
                    "JOIN dialogue source ON source.id = link.choice_row_id "
                    "WHERE link.selected_row_id = d.id AND source.translation = '' "
                    "AND source.translation_status <> 'error') "
                    "ORDER BY d.display_order, d.id LIMIT ?",
                    (limit,),
                )
            ]

    def preloaded_untranslated_ids(
        self, quest: str, limit: int = 10000
    ) -> list[int]:
        with self._lock:
            return [
                int(row[0])
                for row in self._db.execute(
                    "SELECT id FROM dialogue WHERE quest = ? AND preloaded = 1 "
                    "AND translation = '' AND translation_status NOT IN ('translating', 'skipped') "
                    "ORDER BY display_order, id LIMIT ?",
                    (quest, limit),
                )
            ]

    def latest_preloaded_untranslated_ids(self, limit: int = 10000) -> list[int]:
        """Return hidden pending rows only for the most recently staged scenario."""
        with self._lock:
            row = self._db.execute(
                "SELECT quest FROM dialogue WHERE preloaded = 1 "
                "ORDER BY captured_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return []
        return self.preloaded_untranslated_ids(str(row[0]), limit)

    def preloaded_stage_metrics(self, quest: str) -> tuple[int, int]:
        """Return stable whole-stage row/character totals for batch planning."""
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(text)), 0) FROM dialogue "
                "WHERE quest = ? AND preloaded = 1",
                (normalize_text(quest) or "未知关卡",),
            ).fetchone()
        return int(row[0]), int(row[1])

    def mark_translating(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        placeholders = ",".join("?" for _ in row_ids)
        with self._lock:
            self._db.execute(
                f"UPDATE dialogue SET translation_status = 'translating' "
                f"WHERE id IN ({placeholders}) AND translation = ''",
                tuple(row_ids),
            )
            self._db.commit()

    def mark_translation_error(self, row_ids: list[int]) -> None:
        if not row_ids:
            return
        placeholders = ",".join("?" for _ in row_ids)
        with self._lock:
            self._db.execute(
                f"UPDATE dialogue SET translation_status = 'error' "
                f"WHERE id IN ({placeholders}) AND translation = ''",
                tuple(row_ids),
            )
            self._db.commit()

    def reset_translation_errors(self) -> list[int]:
        with self._lock:
            latest = self._db.execute(
                "SELECT quest FROM dialogue WHERE preloaded = 1 "
                "ORDER BY captured_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if latest:
                rows = self._db.execute(
                    "SELECT id FROM dialogue WHERE translation = '' "
                    "AND translation_status IN ('error', 'translating') "
                    "AND (preloaded = 0 OR (preloaded = 1 AND quest = ?)) "
                    "ORDER BY display_order, id",
                    (str(latest[0]),),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id FROM dialogue WHERE translation = '' "
                    "AND translation_status IN ('error', 'translating') "
                    "AND preloaded = 0 ORDER BY display_order, id"
                ).fetchall()
            ids = [int(row[0]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self._db.execute(
                f"UPDATE dialogue SET translation_status = '' WHERE id IN ({placeholders})",
                tuple(ids),
            )
            self._db.commit()
        return ids

    def translation_context(
        self, quest: str, before_order: int, limit: int = 30
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, speaker, text, translation FROM dialogue "
                "WHERE quest = ? AND display_order < ? AND translation <> '' "
                "AND translation_status <> 'skipped' "
                "ORDER BY display_order DESC, id DESC LIMIT ?",
                (quest, before_order, limit),
            ).fetchall()
        rows.reverse()
        return [
            {"id": int(row[0]), "speaker": str(row[1]), "ja": str(row[2]), "zh": str(row[3])}
            for row in rows
        ]

    def explanation(self, row_id: int) -> dict[str, Any] | None:
        """Return a cached explanation without spending another Codex turn."""
        with self._lock:
            row = self._db.execute(
                "SELECT explanation, model, updated_at, web_enabled "
                "FROM dialogue_explanation "
                "WHERE row_id = ?",
                (int(row_id),),
            ).fetchone()
        if not row:
            return None
        return {
            "explanation": str(row[0]),
            "model": str(row[1]),
            "updated_at": str(row[2]),
            "web_enabled": bool(row[3]),
        }

    def explanation_context(self, row_id: int) -> dict[str, Any] | None:
        """Build the complete same-stage bilingual script around one target row."""
        with self._lock:
            target = self._db.execute(
                "SELECT id, display_order, quest, speaker, text, translation, preloaded "
                "FROM dialogue WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if not target:
                return None
            quest = str(target[2])
            rows = self._db.execute(
                "SELECT id, speaker, text, translation, preloaded FROM dialogue "
                "WHERE quest = ? AND translation_status <> 'skipped' "
                "ORDER BY display_order, id",
                (quest,),
            ).fetchall()
        scenario = [
            {
                "id": int(row[0]),
                "speaker": str(row[1]),
                "ja": str(row[2]),
                "zh": str(row[3]),
                "played": not bool(row[4]),
            }
            for row in rows
        ]
        return {
            "quest": quest,
            "target_id": int(target[0]),
            "target": {
                "id": int(target[0]),
                "speaker": str(target[3]),
                "ja": str(target[4]),
                "zh": str(target[5]),
            },
            "scenario": scenario,
        }

    def save_explanation(
        self,
        row_id: int,
        explanation: str,
        model: str,
        web_enabled: bool = False,
    ) -> None:
        clean = str(explanation).strip()
        if not clean:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO dialogue_explanation "
                "(row_id, explanation, model, updated_at, web_enabled) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(row_id) DO UPDATE SET "
                "explanation = excluded.explanation, model = excluded.model, "
                "updated_at = excluded.updated_at, web_enabled = excluded.web_enabled",
                (
                    int(row_id), clean, str(model),
                    datetime.now().isoformat(timespec="seconds"),
                    1 if web_enabled else 0,
                ),
            )
            self._db.commit()

    def reset_translation_rows(self, row_ids: list[int]) -> None:
        """Return interrupted, untranslated rows to the pending state."""
        if not row_ids:
            return
        placeholders = ",".join("?" for _ in row_ids)
        with self._lock:
            self._db.execute(
                f"UPDATE dialogue SET translation_status = '' "
                f"WHERE id IN ({placeholders}) AND translation = ''",
                tuple(row_ids),
            )
            self._db.commit()

    def view_counts(
        self, quest: str | None = None, include_preloaded: bool = False
    ) -> tuple[int, int]:
        """Return table totals without walking thousands of Tk tree items."""
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if not include_preloaded:
                clauses.append("preloaded = 0")
            if quest:
                clauses.append("quest = ?")
                params.append(quest)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(preloaded), 0) "
                f"FROM dialogue{where}",
                tuple(params),
            ).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _numbered_options(text: str) -> dict[int, str]:
        options: dict[int, str] = {}
        current: int | None = None
        for line in normalize_text(text).splitlines():
            match = re.match(r"^\s*(\d+)[.．、]\s*(.*)$", line)
            if match:
                current = int(match.group(1)) - 1
                options[current] = match.group(2).strip()
            elif current is not None and line.strip():
                options[current] += "\n" + line.strip()
        return options

    def link_selected_choice(
        self,
        selected_row_id: int,
        quest: str,
        choice_index: int,
        choice_text: str,
        before_order: int,
    ) -> bool:
        clean_quest = normalize_text(quest) or "未知关卡"
        wanted = comparison_text(choice_text)
        with self._lock:
            candidates = self._db.execute(
                "SELECT id, text, translation, translation_model FROM dialogue "
                "WHERE quest = ? AND preloaded = 0 AND speaker = '【剧情选项】' "
                "AND display_order < ? ORDER BY display_order DESC, id DESC LIMIT 8",
                (clean_quest, int(before_order)),
            ).fetchall()
            source: tuple[Any, ...] | None = None
            matched_index = int(choice_index)
            for row in candidates:
                options = self._numbered_options(str(row[1]))
                if matched_index in options and comparison_text(options[matched_index]) == wanted:
                    source = row
                    break
                exact = [index for index, value in options.items() if comparison_text(value) == wanted]
                if exact:
                    matched_index = exact[0]
                    source = row
                    break
            if source is None:
                return False
            self._db.execute(
                "INSERT INTO choice_translation_link "
                "(selected_row_id, choice_row_id, choice_index) VALUES (?, ?, ?) "
                "ON CONFLICT(selected_row_id) DO UPDATE SET "
                "choice_row_id = excluded.choice_row_id, choice_index = excluded.choice_index",
                (int(selected_row_id), int(source[0]), matched_index),
            )
            translated_options = self._numbered_options(str(source[2]))
            translated = normalize_text(translated_options.get(matched_index, ""))
            if translated:
                self._db.execute(
                    "UPDATE dialogue SET translation = ?, translation_model = ?, "
                    "translation_status = 'done', translation_updated_at = ? WHERE id = ?",
                    (
                        translated,
                        str(source[3]),
                        datetime.now().isoformat(timespec="seconds"),
                        int(selected_row_id),
                    ),
                )
            self._db.commit()
            return True

    def _propagate_choice_translation_locked(
        self, choice_row_id: int, translation: str, model: str, now: str
    ) -> list[dict[str, Any]]:
        options = self._numbered_options(translation)
        propagated: list[dict[str, Any]] = []
        for selected_row_id, choice_index in self._db.execute(
            "SELECT selected_row_id, choice_index FROM choice_translation_link "
            "WHERE choice_row_id = ?",
            (int(choice_row_id),),
        ).fetchall():
            selected_translation = normalize_text(options.get(int(choice_index), ""))
            if not selected_translation:
                continue
            cur = self._db.execute(
                "UPDATE dialogue SET translation = ?, translation_model = ?, "
                "translation_status = 'done', translation_updated_at = ? "
                "WHERE id = ? AND translation = ''",
                (selected_translation, model, now, int(selected_row_id)),
            )
            if cur.rowcount:
                propagated.append(
                    {"id": int(selected_row_id), "translation": selected_translation}
                )
        return propagated

    def choice_dependents(self, choice_row_ids: list[int]) -> list[int]:
        clean_ids = sorted({int(value) for value in choice_row_ids if int(value) > 0})
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        with self._lock:
            return [
                int(row[0])
                for row in self._db.execute(
                    "SELECT link.selected_row_id FROM choice_translation_link link "
                    "JOIN dialogue selected ON selected.id = link.selected_row_id "
                    f"WHERE link.choice_row_id IN ({placeholders}) "
                    "AND selected.translation = '' ORDER BY selected.display_order, selected.id",
                    tuple(clean_ids),
                ).fetchall()
            ]

    def _propagate_preload_compositions_locked(
        self, fragment_ids: list[int], fallback_model: str, now: str
    ) -> list[dict[str, Any]]:
        clean_ids = sorted({int(value) for value in fragment_ids if int(value) > 0})
        if not clean_ids:
            return []
        placeholders = ",".join("?" for _ in clean_ids)
        live_ids = [
            int(row[0])
            for row in self._db.execute(
                "SELECT DISTINCT live_row_id FROM preload_composition_link "
                f"WHERE fragment_row_id IN ({placeholders})",
                tuple(clean_ids),
            ).fetchall()
        ]
        propagated: list[dict[str, Any]] = []
        for live_id in live_ids:
            fragments = self._db.execute(
                "SELECT fragment.translation, fragment.translation_model, "
                "fragment.translation_updated_at FROM preload_composition_link link "
                "JOIN dialogue fragment ON fragment.id = link.fragment_row_id "
                "WHERE link.live_row_id = ? ORDER BY link.fragment_order",
                (live_id,),
            ).fetchall()
            expected = int(
                self._db.execute(
                    "SELECT COUNT(*) FROM preload_composition_link WHERE live_row_id = ?",
                    (live_id,),
                ).fetchone()[0]
            )
            if not fragments or len(fragments) != expected or not all(str(x[0]) for x in fragments):
                continue
            translation = "".join(str(item[0]) for item in fragments)
            model = next((str(item[1]) for item in fragments if str(item[1])), fallback_model)
            updated = max([str(item[2]) for item in fragments if str(item[2])] or [now])
            cur = self._db.execute(
                "UPDATE dialogue SET translation = ?, translation_model = ?, "
                "translation_status = 'done', translation_updated_at = ? "
                "WHERE id = ? AND translation = ''",
                (translation, model, updated, live_id),
            )
            if cur.rowcount:
                propagated.append({"id": live_id, "translation": translation})
            self._db.execute(
                "DELETE FROM preload_composition_link WHERE live_row_id = ?", (live_id,)
            )
        return propagated

    def save_translations(
        self, values: list[dict[str, Any]], model: str
    ) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            translated_source_ids: list[int] = []
            for value in values:
                try:
                    row_id = int(value.get("id"))
                except (TypeError, ValueError):
                    continue
                translated = normalize_text(str(value.get("zh", "")))
                if not translated:
                    continue
                source_row = self._db.execute(
                    "SELECT quest, speaker, text FROM dialogue WHERE id = ?", (row_id,)
                ).fetchone()
                if not source_row:
                    continue
                translated = enforce_character_style_translation(
                    str(source_row[1]), str(source_row[2]), translated
                )
                source_speaker = sanitize_speaker(str(source_row[1]))
                speaker_translation = normalize_text(
                    str(value.get("speaker_zh", ""))
                )[:120]
                if (
                    source_speaker
                    and not (
                        source_speaker.startswith("【")
                        and source_speaker.endswith("】")
                    )
                    and speaker_translation
                ):
                    self._db.execute(
                        "INSERT INTO speaker_name_translation "
                        "(quest, speaker, translation, source, updated_at) "
                        "VALUES (?, ?, ?, 'model', ?) "
                        "ON CONFLICT(quest, speaker) DO UPDATE SET "
                        "translation = excluded.translation, source = excluded.source, "
                        "updated_at = excluded.updated_at",
                        (str(source_row[0]), source_speaker, speaker_translation, now),
                    )
                cur = self._db.execute(
                    "UPDATE dialogue SET translation = ?, translation_model = ?, "
                    "translation_status = 'done', translation_updated_at = ? WHERE id = ?",
                    (translated, model, now, row_id),
                )
                if cur.rowcount:
                    translated_source_ids.append(row_id)
                    saved_item: dict[str, Any] = {
                        "id": row_id,
                        "translation": translated,
                    }
                    if speaker_translation:
                        saved_item["speaker_translation"] = speaker_translation
                    saved.append(saved_item)
                    saved.extend(
                        self._propagate_choice_translation_locked(
                            row_id, translated, model, now
                        )
                    )
            saved.extend(
                self._propagate_preload_compositions_locked(
                    translated_source_ids, model, now
                )
            )
            self._db.commit()
        return saved

    def speaker_translation(self, quest: str, speaker: str) -> str:
        clean_quest = normalize_text(quest) or "未知关卡"
        clean_speaker = sanitize_speaker(speaker)
        if not clean_speaker:
            return ""
        with self._lock:
            row = self._db.execute(
                "SELECT translation FROM speaker_name_translation "
                "WHERE quest = ? AND speaker = ?",
                (clean_quest, clean_speaker),
            ).fetchone()
        return normalize_text(str(row[0])) if row else ""

    def quest_memory(self, quest: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT memory_key, memory_value, evidence FROM quest_memory "
                "WHERE quest = ? ORDER BY memory_key",
                (quest,),
            ).fetchall()
        return [
            {"key": str(row[0]), "value": str(row[1]), "evidence": str(row[2])}
            for row in rows
        ]

    def save_quest_memory(self, quest: str, values: list[dict[str, Any]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        clean: list[tuple[str, str, str, str, str]] = []
        for value in values[:30]:
            key = normalize_text(str(value.get("key", "")))[:100]
            memory_value = normalize_text(str(value.get("value", "")))[:300]
            evidence_ids = value.get("evidence_line_ids", [])
            if not isinstance(evidence_ids, list):
                evidence_ids = []
            candidates = [int(item) for item in evidence_ids if str(item).isdigit()]
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                with self._lock:
                    proven = {
                        int(row[0])
                        for row in self._db.execute(
                            f"SELECT id FROM dialogue WHERE quest = ? AND id IN ({placeholders})",
                            (quest, *candidates),
                        )
                    }
            else:
                proven = set()
            evidence = ",".join(str(item) for item in candidates if item in proven)
            if key and memory_value and evidence:
                clean.append((quest, key, memory_value, evidence, now))
        if not clean:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO quest_memory "
                "(quest, memory_key, memory_value, evidence, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(quest, memory_key) DO UPDATE SET "
                "memory_value = excluded.memory_value, evidence = excluded.evidence, "
                "updated_at = excluded.updated_at",
                clean,
            )
            self._db.commit()

    def speaker_styles(self, lines: list[dict[str, Any]]) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        with self._lock:
            rows = self._db.execute(
                "SELECT speaker, source_pattern, zh_rendering FROM speaker_style_memory "
                "WHERE status = 'active' ORDER BY speaker, source_pattern"
            ).fetchall()
        for line in lines:
            speaker = sanitize_speaker(str(line.get("speaker", "")))
            japanese = normalize_text(str(line.get("ja", line.get("text", ""))))
            for row_speaker, pattern, rendering in rows:
                key = (str(row_speaker), str(pattern), str(rendering))
                if key in seen or speaker != key[0] or key[1] not in japanese:
                    continue
                seen.add(key)
                output.append(
                    {
                        "speaker": key[0],
                        "source_marker": key[1],
                        "preferred_zh_rendering": key[2],
                        "status": "runtime_evidence",
                    }
                )
        return output[:24]

    def save_speaker_styles(
        self, values: list[dict[str, Any]], allowed_ids: set[int]
    ) -> None:
        if not values or not allowed_ids:
            return
        common_patterns = {
            "です", "ます", "でした", "ません", "でしょう", "ましょう", "だよ", "だね",
            "だな", "なの", "ので", "から", "けど", "かな", "よね", "いる", "ある",
            "ない", "たい", "れる", "られる", "する", "した", "して",
        }
        common_renderings = {"了", "吗", "呢", "吧", "啊", "呀", "哦", "啦", "的", "是"}
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            for value in values[:30]:
                speaker = sanitize_speaker(str(value.get("speaker", "")))[:80]
                pattern = normalize_text(str(value.get("source_pattern", "")))[:16]
                rendering = normalize_text(str(value.get("zh_rendering", "")))[:12]
                evidence_raw = value.get("evidence_line_ids", [])
                evidence_ids = {
                    int(item) for item in evidence_raw
                    if str(item).isdigit() and int(item) in allowed_ids
                } if isinstance(evidence_raw, list) else set()
                if (
                    not speaker or len(pattern) < 2 or not rendering
                    or pattern in common_patterns or rendering in common_renderings
                    or not evidence_ids
                ):
                    continue
                placeholders = ",".join("?" for _ in evidence_ids)
                evidence_rows = self._db.execute(
                    "SELECT id, speaker, text, translation FROM dialogue "
                    f"WHERE id IN ({placeholders})",
                    tuple(evidence_ids),
                ).fetchall()
                proven_ids: set[int] = set()
                proven_texts: set[str] = set()
                for row_id, row_speaker, japanese, translated in evidence_rows:
                    if (
                        sanitize_speaker(str(row_speaker)) == speaker
                        and pattern in normalize_text(str(japanese))
                        and rendering in normalize_text(str(translated))
                    ):
                        proven_ids.add(int(row_id))
                        proven_texts.add(normalize_text(str(japanese)))
                if not proven_ids:
                    continue
                old = self._db.execute(
                    "SELECT evidence_ids, evidence_texts FROM speaker_style_memory "
                    "WHERE speaker = ? AND source_pattern = ? AND zh_rendering = ?",
                    (speaker, pattern, rendering),
                ).fetchone()
                if old:
                    try:
                        proven_ids.update(int(item) for item in json.loads(str(old[0])))
                        proven_texts.update(str(item) for item in json.loads(str(old[1])))
                    except (ValueError, TypeError):
                        pass
                status = "active" if len(proven_texts) >= 3 else "candidate"
                # Never let two different Chinese renderings become active for
                # the same speaker/source marker.  A later disagreement is
                # quarantined for manual review instead of silently replacing
                # a rule that is already affecting translations.
                if status == "active":
                    competing = self._db.execute(
                        "SELECT 1 FROM speaker_style_memory "
                        "WHERE speaker = ? AND source_pattern = ? AND zh_rendering <> ? "
                        "AND status = 'active' LIMIT 1",
                        (speaker, pattern, rendering),
                    ).fetchone()
                    if competing:
                        status = "conflict"
                self._db.execute(
                    "INSERT INTO speaker_style_memory "
                    "(speaker, source_pattern, zh_rendering, evidence_ids, evidence_texts, status, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(speaker, source_pattern, zh_rendering) DO UPDATE SET "
                    "evidence_ids = excluded.evidence_ids, evidence_texts = excluded.evidence_texts, "
                    "status = excluded.status, updated_at = excluded.updated_at",
                    (
                        speaker, pattern, rendering,
                        json.dumps(sorted(proven_ids), ensure_ascii=False),
                        json.dumps(sorted(proven_texts), ensure_ascii=False),
                        status, now,
                    ),
                )
                if status == "active":
                    self._db.execute(
                        "UPDATE speaker_style_memory SET status = 'conflict' "
                        "WHERE speaker = ? AND source_pattern = ? AND zh_rendering <> ? "
                        "AND status = 'candidate'",
                        (speaker, pattern, rendering),
                    )
            self._db.commit()

    def quests(self, include_preloaded: bool = False) -> list[str]:
        with self._lock:
            where = "quest <> ''" if include_preloaded else "preloaded = 0 AND quest <> ''"
            return [
                str(row[0])
                for row in self._db.execute(
                    "SELECT DISTINCT quest FROM dialogue "
                    f"WHERE {where} ORDER BY quest"
                )
            ]

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM dialogue_explanation")
            self._db.execute("DELETE FROM dialogue")
            self._db.execute("DELETE FROM quest_memory")
            self._db.execute("DELETE FROM speaker_style_memory")
            self._db.execute("DELETE FROM speaker_name_translation")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class RuntimeConfig:
    adb_path: str = ""
    serial: str = "127.0.0.1:5556"
    emulator_index: int = 0
    translation_enabled: bool = True
    translation_model: str = DEFAULT_TRANSLATION_MODEL
    translation_reasoning: str = "low"
    preload_translation_model: str = DEFAULT_PRELOAD_TRANSLATION_MODEL
    preload_translation_reasoning: str = "high"
    preload_translation_fast_mode: bool = True
    translation_batch_size: int = 100
    translation_debounce_ms: int = 120
    translation_engine: str = "app-server"
    translation_fast_mode: bool = True
    show_translation_overlay: bool = True
    pretranslate_scenario: bool = True
    explanation_web_search: bool = True
    codex_path: str = ""

    @classmethod
    def load(cls) -> "RuntimeConfig":
        path = data_dir() / "config.json"
        if not path.exists():
            return cls()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            # v2.8 splits the former single profile. Existing installations used
            # Sol for every line; migrate them to the new low-latency live
            # profile while keeping Sol/high for whole-stage pretranslation.
            split_profiles = "preload_translation_model" in value
            debounce_default = 120
            debounce_value = (
                value.get("translation_debounce_ms", debounce_default)
                if "translation_engine" in value
                else debounce_default
            )
            return cls(
                adb_path=str(value.get("adb_path", "")),
                serial=str(value.get("serial", "127.0.0.1:5556")),
                emulator_index=int(value.get("emulator_index", 0)),
                translation_enabled=bool(value.get("translation_enabled", True)),
                translation_model=str(
                    value.get("translation_model", DEFAULT_TRANSLATION_MODEL)
                    if split_profiles else DEFAULT_TRANSLATION_MODEL
                ),
                translation_reasoning=str(
                    value.get("translation_reasoning", "low")
                    if split_profiles else "low"
                ),
                preload_translation_model=str(
                    value.get(
                        "preload_translation_model", DEFAULT_PRELOAD_TRANSLATION_MODEL
                    )
                ),
                preload_translation_reasoning=str(
                    value.get("preload_translation_reasoning", "high")
                ),
                preload_translation_fast_mode=bool(
                    value.get("preload_translation_fast_mode", True)
                ),
                translation_batch_size=max(1, min(100, int(value.get("translation_batch_size", 100)))),
                translation_debounce_ms=max(50, min(3000, int(debounce_value))),
                translation_engine=str(value.get("translation_engine", "app-server")),
                translation_fast_mode=bool(value.get("translation_fast_mode", True)),
                show_translation_overlay=bool(value.get("show_translation_overlay", True)),
                pretranslate_scenario=bool(value.get("pretranslate_scenario", True)),
                explanation_web_search=bool(
                    value.get("explanation_web_search", True)
                ),
                codex_path=str(value.get("codex_path", "")),
            )
        except Exception:
            return cls()

    def save(self) -> None:
        (data_dir() / "config.json").write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class TranslationSuperseded(RuntimeError):
    pass


class StreamingTranslationParser:
    """Extract completed objects from the streamed `translations` JSON array."""

    def __init__(self) -> None:
        self.buffer = ""
        self.position: int | None = None
        self.seen: set[int] = set()

    def current_preview(self) -> tuple[int, str] | None:
        """Return the currently streamed row's decodable `zh` prefix."""
        if self.position is None:
            return None
        position = self.position
        while position < len(self.buffer) and self.buffer[position] in " \r\n\t,":
            position += 1
        if position >= len(self.buffer) or self.buffer[position] != "{":
            return None
        fragment = self.buffer[position:]
        id_match = re.search(r'"id"\s*:\s*(\d+)', fragment)
        zh_match = re.search(r'"zh"\s*:\s*"', fragment)
        if not id_match or not zh_match:
            return None
        raw = fragment[zh_match.end():]
        decoded: list[str] = []
        index = 0
        escapes = {
            '"': '"', "\\": "\\", "/": "/", "b": "\b",
            "f": "\f", "n": "\n", "r": "\r", "t": "\t",
        }
        while index < len(raw):
            char = raw[index]
            if char == '"':
                break
            if char != "\\":
                decoded.append(char)
                index += 1
                continue
            if index + 1 >= len(raw):
                break
            escape = raw[index + 1]
            if escape == "u":
                digits = raw[index + 2:index + 6]
                if len(digits) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    break
                decoded.append(chr(int(digits, 16)))
                index += 6
                continue
            if escape not in escapes:
                break
            decoded.append(escapes[escape])
            index += 2
        text = "".join(decoded)
        if not text:
            return None
        return int(id_match.group(1)), text

    def feed(self, delta: str) -> list[dict[str, Any]]:
        self.buffer += delta
        if self.position is None:
            match = re.search(r'"translations"\s*:\s*\[', self.buffer)
            if not match:
                return []
            self.position = match.end()
        found: list[dict[str, Any]] = []
        while self.position < len(self.buffer):
            while self.position < len(self.buffer) and self.buffer[self.position] in " \r\n\t,":
                self.position += 1
            if self.position >= len(self.buffer) or self.buffer[self.position] == "]":
                break
            if self.buffer[self.position] != "{":
                break
            start = self.position
            depth = 0
            in_string = False
            escaped = False
            end: int | None = None
            for index in range(start, len(self.buffer)):
                char = self.buffer[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        end = index + 1
                        break
            if end is None:
                break
            try:
                value = json.loads(self.buffer[start:end])
            except ValueError:
                self.position = end
                continue
            self.position = end
            if not isinstance(value, dict):
                continue
            try:
                row_id = int(value.get("id"))
            except (TypeError, ValueError):
                continue
            if row_id not in self.seen:
                self.seen.add(row_id)
                found.append(value)
        return found


class CodexAppServerClient:
    """Small JSON-RPC client that keeps Codex and one thread per quest warm."""

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.deferred: deque[dict[str, Any]] = deque()
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self.request_id = 0
        self.quest_threads: dict[str, str] = {}
        self._write_lock = threading.Lock()

    def has_thread(self, quest: str) -> bool:
        return quest in self.quest_threads

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        command = [
            *_codex_command(self.config.codex_path),
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            f'model_reasoning_effort="{self.config.translation_reasoning}"',
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.messages = queue.Queue()
        self.deferred.clear()
        self.stderr_tail.clear()
        self.quest_threads.clear()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        process = self.process
        message_queue = self.messages
        stderr_tail = self.stderr_tail

        def read_stdout() -> None:
            assert process.stdout
            for line in process.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        message_queue.put(value)
                except ValueError:
                    continue
            message_queue.put({"__server_closed__": True})

        def read_stderr() -> None:
            assert process.stderr
            for line in process.stderr:
                stderr_tail.append(line.rstrip())

        threading.Thread(target=read_stdout, name="CodexAppServerOut", daemon=True).start()
        threading.Thread(target=read_stderr, name="CodexAppServerErr", daemon=True).start()
        response = self._request(
            "initialize",
            {
                "clientInfo": {"name": "fgo-story-listener", "version": APP_VERSION},
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        if "error" in response:
            raise RuntimeError(str(response["error"].get("message", response["error"])))
        self._send({"method": "initialized"})

    def _send(self, value: dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise RuntimeError("Codex 常驻服务未运行。")
        with self._write_lock:
            self.process.stdin.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self.process.stdin.flush()

    def _request(
        self, method: str, params: dict[str, Any] | None = None, timeout: int = 60
    ) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        value: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            value["params"] = params
        self._send(value)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                message = self.messages.get(timeout=min(0.5, max(0.01, deadline - time.time())))
            except queue.Empty:
                continue
            if message.get("__server_closed__"):
                tail = "\n".join(self.stderr_tail)
                raise RuntimeError(f"Codex 常驻服务意外退出。\n{tail}")
            if message.get("id") == request_id:
                return message
            self.deferred.append(message)
        raise RuntimeError(f"Codex 常驻服务请求超时：{method}")

    def _base_instructions(self) -> str:
        return (
            "你是 Fate/Grand Order 日文剧情的专业简体中文本地化译者。"
            "你只做翻译，不使用任何工具，不浏览网页，不读写文件。"
            "translations 必须严格保持输入对象顺序，使关卡开头可以先流式显示。"
            "每个输入对象都有 id、speaker、ja；speaker_zh 填写 speaker 的简体中文名称，"
            "speaker 为空时 speaker_zh 必须为空；不得给旁白、场景描写或内心文字制造人物名。"
            "zh 只填写 ja 的译文，"
            "不要擅自在译文前添加人物名或外层引号。不得漏行、合并、虚构或改变剧情事实。"
            "保留语气、口癖、分行和选项编号；同一关卡称呼保持一致。"
            "未知昵称保守翻译，不冒充官方译名。memory_updates 只记录当前台词明确证明且带证据 id 的"
            "关卡内称呼/关系/临时术语。每一轮输入的 curated_glossary 是按当前台词检索出的"
            "来源可追溯术语硬约束，character_style_guide 是人物专属口癖硬约束。"
            "scenario_preload_mode 为 true 时不得输出 memory_updates，避免未选择分支污染关卡记忆。"
            "speaker_style_memory 是至少三条不同台词验证过的一致性偏好。style_updates 可提交"
            "当前行中清楚可辨的人物专属非标准口癖候选；程序会累计证据且仅在三条不同台词后启用。"
            "不得把普通日语语尾当作口癖，并必须附当前输入的证据 id。"
            "最终只输出符合 turn outputSchema 的 JSON。"
        )

    def _ensure_thread(self, quest: str, runtime: Path) -> str:
        existing = self.quest_threads.get(quest)
        if existing:
            return existing
        params: dict[str, Any] = {
            "model": self.config.translation_model,
            "cwd": str(runtime),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "baseInstructions": self._base_instructions(),
            "dynamicTools": [],
            "personality": "none",
            "config": {"model_reasoning_effort": self.config.translation_reasoning},
        }
        if fast_mode_effective(self.config):
            params["serviceTier"] = "priority"
        response = self._request("thread/start", params, timeout=60)
        if "error" in response:
            raise RuntimeError(str(response["error"].get("message", response["error"])))
        thread_id = str(response["result"]["thread"]["id"])
        self.quest_threads[quest] = thread_id
        return thread_id

    def translate(
        self,
        quest: str,
        prompt: str,
        output_schema: dict[str, Any],
        runtime: Path,
        cancel_event: threading.Event | None = None,
        partial_callback: Callable[[dict[str, Any]], None] | None = None,
        preview_callback: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        self.start()
        thread_id = self._ensure_thread(quest, runtime)
        params: dict[str, Any] = {
            "threadId": thread_id,
            "model": self.config.translation_model,
            "effort": self.config.translation_reasoning,
            "input": [{"type": "text", "text": prompt}],
            "outputSchema": output_schema,
        }
        if fast_mode_effective(self.config):
            params["serviceTier"] = "priority"
        response = self._request("turn/start", params, timeout=60)
        if "error" in response:
            raise RuntimeError(str(response["error"].get("message", response["error"])))
        turn_id = str(response["result"]["turn"]["id"])
        deltas: list[str] = []
        stream_parser = StreamingTranslationParser()
        deadline = time.time() + 180
        interrupt_sent = False
        interrupt_deadline = 0.0
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set() and not interrupt_sent:
                self.request_id += 1
                self._send(
                    {
                        "id": self.request_id,
                        "method": "turn/interrupt",
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    }
                )
                interrupt_sent = True
                interrupt_deadline = time.time() + 0.8
            if interrupt_sent and time.time() >= interrupt_deadline:
                self.close()
                raise TranslationSuperseded("旧句翻译已被当前句抢占。")
            if self.deferred:
                message = self.deferred.popleft()
            else:
                try:
                    message = self.messages.get(timeout=0.05)
                except queue.Empty:
                    continue
            if message.get("__server_closed__"):
                raise RuntimeError("Codex 常驻服务在翻译过程中退出。")
            method = message.get("method")
            params = message.get("params", {})
            if method == "item/agentMessage/delta" and str(params.get("turnId")) == turn_id:
                delta = str(params.get("delta", ""))
                deltas.append(delta)
                if partial_callback is not None:
                    for item in stream_parser.feed(delta):
                        partial_callback(item)
                else:
                    stream_parser.feed(delta)
                if preview_callback is not None:
                    preview = stream_parser.current_preview()
                    if preview is not None:
                        preview_callback(*preview)
            elif method == "turn/completed" and str(params.get("turn", {}).get("id")) == turn_id:
                turn = params.get("turn", {})
                if turn.get("status") != "completed":
                    if interrupt_sent or (cancel_event is not None and cancel_event.is_set()):
                        raise TranslationSuperseded("旧句翻译已被当前句抢占。")
                    error = turn.get("error") or {}
                    raise RuntimeError(str(error.get("message", "Codex 翻译未完成。")))
                raw = "".join(deltas).strip()
                if not raw:
                    raise RuntimeError("Codex 常驻服务没有返回译文。")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("翻译结果不是 JSON 对象")
                return value
        raise RuntimeError("Codex 常驻翻译超过 180 秒。")

    def close(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.quest_threads.clear()


class CodexTranslationWorker(threading.Thread):
    """Translate captured rows in quest-scoped batches through local codex-cli."""

    def __init__(
        self,
        store: HistoryStore,
        config: RuntimeConfig,
        events: queue.Queue[dict[str, Any]],
        scope: str = "all",
    ):
        if scope not in {"all", "live", "preload"}:
            raise ValueError(f"未知翻译队列范围：{scope}")
        super().__init__(
            name="CodexPreloadTranslator" if scope == "preload" else "CodexTranslator",
            daemon=True,
        )
        self.store = store
        self.config = config
        self.events = events
        self.scope = scope
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self._lock = threading.Lock()
        self._pending: set[int] = set()
        self._urgent: set[int] = set()
        self._active_ids: set[int] = set()
        self._active_cancel: threading.Event | None = None
        self._whole_stage_decisions: dict[str, bool] = {}
        self._process: subprocess.Popen[str] | None = None
        self._knowledge = KnowledgeBase(
            data_dir() / "knowledge", bundled_knowledge_path()
        )
        # Preload and live translation must never share a model/thread.  The
        # preload worker owns a second warm app-server configured for Sol,
        # while the live worker remains free to serve the current sentence.
        client_config = RuntimeConfig(**config.__dict__)
        if scope == "preload":
            client_config.translation_model = config.preload_translation_model
            client_config.translation_reasoning = config.preload_translation_reasoning
            client_config.translation_fast_mode = config.preload_translation_fast_mode
        self._app_server = CodexAppServerClient(client_config)
        self._fast_mode_failed = False

    def enqueue(self, row_ids: list[int], urgent: bool = False) -> None:
        with self._lock:
            clean = {int(value) for value in row_ids if int(value) > 0}
            self._pending.update(clean)
            if urgent:
                self._urgent.update(clean)
        self.wakeup.set()

    def enqueue_current(self, row_id: int) -> None:
        """Put only the newest visible page in the latency-critical lane.

        Older live rows remain pending for history completion, but they must
        never make the sentence currently on screen wait behind them.
        """
        row_id = int(row_id)
        if row_id <= 0:
            return
        with self._lock:
            self._pending.add(row_id)
            self._urgent.clear()
            self._urgent.add(row_id)
            # If a stale live line is still consuming the sole live turn,
            # interrupt it immediately.  It is requeued after the visible row
            # has been served, so history completeness is preserved.
            if self._active_cancel is not None and row_id not in self._active_ids:
                self._active_cancel.set()
        self.wakeup.set()

    def enqueue_all_pending(self) -> None:
        self.enqueue(
            [
                *self.store.untranslated_ids(),
                *self.store.latest_preloaded_untranslated_ids(),
            ]
        )

    def _take_batch(self) -> list[dict[str, Any]]:
        with self._lock:
            urgent_ids = list(self._urgent)
            ids = urgent_ids or list(self._pending)
        items = [
            item
            for item in self.store.translation_items(ids)
            if not item["translation"]
            and item.get("status") != "skipped"
            and not item.get("choice_waiting")
            and (
                self.scope == "all"
                or bool(item.get("preloaded")) == (self.scope == "preload")
            )
        ]
        if not items:
            with self._lock:
                self._pending.difference_update(ids)
                self._urgent.difference_update(ids)
            return []
        first_item = items[-1 if urgent_ids else 0]
        quest = str(first_item["quest"])
        first_is_preload = bool(first_item.get("preloaded"))
        # Never mix staged and live rows in one request: they deliberately use
        # different models/reasoning profiles from v2.8 onward.
        quest_items = [
            item for item in items
            if str(item["quest"]) == quest
            and bool(item.get("preloaded")) == first_is_preload
        ]
        contains_preload = first_is_preload
        limit = self.config.translation_batch_size
        if not urgent_ids and contains_preload:
            # Prefer one coherent request for a normal FGO stage.  A row-only
            # threshold is unsafe because one script slot can be much longer
            # than another, so require both row and Japanese-character budgets.
            # Larger stages retain the proven 100-row fallback. Streaming
            # persistence means early rows become usable before the full turn
            # completes in either mode.
            whole_stage = self._whole_stage_decisions.get(quest)
            if whole_stage is None:
                stage_rows, stage_chars = self.store.preloaded_stage_metrics(quest)
                whole_stage = (
                    stage_rows <= WHOLE_STAGE_MAX_ROWS
                    and stage_chars <= WHOLE_STAGE_MAX_JAPANESE_CHARS
                )
                self._whole_stage_decisions[quest] = whole_stage
            if whole_stage:
                limit = len(quest_items)
            else:
                limit = 100
        elif not urgent_ids and self.config.translation_engine == "app-server":
            limit = min(limit, 12)
        batch = quest_items[:limit]
        batch_ids = [int(item["id"]) for item in batch]
        with self._lock:
            self._pending.difference_update(batch_ids)
            self._urgent.difference_update(batch_ids)
        return batch

    def _profile(self, is_preload: bool) -> tuple[str, str, bool]:
        if is_preload:
            return (
                self.config.preload_translation_model,
                self.config.preload_translation_reasoning,
                fast_mode_effective(self.config, preloaded=True),
            )
        return (
            self.config.translation_model,
            self.config.translation_reasoning,
            fast_mode_effective(self.config),
        )

    def _build_prompt(
        self,
        batch: list[dict[str, Any]],
        include_static_context: bool = True,
        persistent_mode: bool = False,
    ) -> str:
        quest = str(batch[0]["quest"])
        context = (
            self.store.translation_context(
                quest, min(int(item["display_order"]) for item in batch), 30
            )
            if include_static_context
            else []
        )
        memory = self.store.quest_memory(quest)
        lines = [
            {"id": item["id"], "speaker": item["speaker"], "ja": item["text"]}
            for item in batch
        ]
        payload: dict[str, Any] = {"lines_to_translate": lines}
        is_preload = any(bool(item.get("preloaded")) for item in batch)
        if is_preload:
            payload["scenario_preload_mode"] = True
        # Omit every empty optional section. Retrieve only terms that actually
        # occur in the current lines/context, so neither the quest title nor a
        # multi-thousand-entry dictionary consumes per-turn tokens.
        relevant_terms = self._knowledge.relevant_terms(lines, context)
        if relevant_terms:
            payload["curated_glossary"] = relevant_terms
        style_guide = [
            *character_style_guide(lines),
            *self._knowledge.relevant_styles(lines),
        ]
        if style_guide:
            unique_styles: list[dict[str, Any]] = []
            seen_styles: set[tuple[str, str, str]] = set()
            for style in style_guide:
                marker = str(style.get("source_marker", "")) or ",".join(
                    str(item) for item in style.get("source_markers", [])
                )
                key = (
                    str(style.get("speaker", "")), marker,
                    str(style.get("required_zh_rendering", "")),
                )
                if key not in seen_styles:
                    seen_styles.add(key)
                    unique_styles.append(style)
            payload["character_style_guide"] = unique_styles
        learned_styles = self.store.speaker_styles(lines)
        if learned_styles:
            payload["speaker_style_memory"] = learned_styles
        if memory:
            payload["quest_memory"] = memory
        if context:
            payload["previous_bilingual_context"] = context
        if persistent_mode:
            return (
                "翻译 lines_to_translate；上下文仅参考，勿重复。"
                "每项同时返回 speaker_zh；无人物名时返回空字符串。"
                "zh 勿加人名或外层引号；只输出 Schema JSON。\n"
                + json.dumps(payload, ensure_ascii=False)
            )
        return (
            "你是 Fate/Grand Order 日文剧情的专业简体中文本地化译者。"
            "只完成翻译，不使用任何工具，不浏览网页，不修改文件。\n"
            "要求：\n"
            "1. 完整翻译 lines_to_translate 中每个 id，不得漏行、合并、虚构或改写剧情事实。\n"
            "translations 必须严格保持 lines_to_translate 的输入顺序，使开头剧情可以先流式显示。\n"
            "每项 speaker_zh 翻译人物名；speaker 为空时必须返回空字符串，不得虚构旁白人名。\n"
            "2. curated_glossary 和 character_style_guide 是经来源校对的硬约束；"
            "同一关卡的人名、称呼、口癖保持一致。\n"
            "3. previous_bilingual_context 和 quest_memory 只用于理解上下文，不要重复输出旧行；"
            "scenario_preload_mode 为 true 时 memory_updates 必须返回空数组。\n"
            "4. 保留省略号、语气、分行、选项编号；中文自然，但不要把人物说话风格抹平。\n"
            "5. 不确定的新昵称采用保守译法，不得冒充官方译名。\n"
            "6. memory_updates 只能记录本关卡已由当前行明确证明的称呼/人物关系/临时术语，"
            "必须附 evidence_line_ids；无可靠新信息就返回空数组。\n"
            "7. style_updates 可提交当前行中清楚可辨的人物独有非标准口癖候选及其中文呈现；"
            "程序会跨批累计，并只在三条不同日文台词验证后启用。不得记录です、ます等普通语尾；"
            "必须附当前输入的 evidence_line_ids，无可靠候选就返回空数组。\n"
            "8. 最终只输出符合给定 JSON Schema 的对象。\n\n"
            "输入数据：\n" + json.dumps(payload, ensure_ascii=False)
        )

    def _invoke_exec(self, prompt: str, is_preload: bool = False) -> dict[str, Any]:
        runtime = data_dir() / "translation-runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        output = runtime / f"result-{uuid.uuid4().hex}.json"
        schema = resource_path("translation_runtime/translation_schema.json")
        if not schema.is_file():
            raise RuntimeError(f"翻译输出结构文件缺失：{schema}")
        model, reasoning, fast = self._profile(is_preload)
        command = [
            *_codex_command(self.config.codex_path),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
        ]
        if fast:
            command.extend(["-c", 'service_tier="priority"'])
        command.extend([
            "--output-schema",
            str(schema),
            "-o",
            str(output),
            "-C",
            str(runtime),
            "-",
        ])
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
            stdout, _ = self._process.communicate(prompt, timeout=180)
            if self._process.returncode != 0:
                tail = "\n".join(stdout.strip().splitlines()[-8:])
                raise RuntimeError(f"codex-cli 翻译失败（{self._process.returncode}）：{tail}")
            if not output.is_file():
                raise RuntimeError("codex-cli 未生成翻译结果。")
            value = json.loads(output.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("翻译结果不是 JSON 对象")
            return value
        except subprocess.TimeoutExpired as exc:
            if self._process:
                self._process.kill()
            raise RuntimeError("AI 翻译超过 180 秒，已停止本批次。") from exc
        finally:
            self._process = None
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass

    def _invoke(
        self,
        quest: str,
        prompt: str,
        fallback_prompt: str | None = None,
        is_preload: bool = False,
        cancel_event: threading.Event | None = None,
        partial_callback: Callable[[dict[str, Any]], None] | None = None,
        preview_callback: Callable[[int, str], None] | None = None,
    ) -> dict[str, Any]:
        # Whole-stage work uses its own worker and its own Sol app-server.
        # Completed objects are persisted while the remaining objects are
        # still streaming, instead of making the first 99 wait for item 100.
        if self.config.translation_engine != "app-server" or self._fast_mode_failed:
            return self._invoke_exec(fallback_prompt or prompt, is_preload=is_preload)
        runtime = data_dir() / "translation-runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        schema_path = resource_path("translation_runtime/translation_schema.json")
        if not schema_path.is_file():
            raise RuntimeError(f"翻译输出结构文件缺失：{schema_path}")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        try:
            return self._app_server.translate(
                quest,
                prompt,
                schema,
                runtime,
                cancel_event=None if is_preload else cancel_event,
                partial_callback=partial_callback,
                preview_callback=None if is_preload else preview_callback,
            )
        except TranslationSuperseded:
            raise
        except Exception as fast_error:
            # A remotely advertised model can still be rejected by an older
            # local Codex protocol implementation. This is not a translation
            # failure: retry the same batch with the known-compatible default
            # and persist that choice so every queued preload is not failed in
            # succession with the same 400 response.
            if (
                not is_preload
                and
                "requires a newer version of Codex" in str(fast_error)
                and self.config.translation_model != COMPATIBLE_TRANSLATION_MODEL
            ):
                rejected_model = self.config.translation_model
                self._app_server.close()
                self.config.translation_model = COMPATIBLE_TRANSLATION_MODEL
                try:
                    self.config.save()
                except OSError:
                    pass
                self._app_server = CodexAppServerClient(self.config)
                self._fast_mode_failed = False
                self.events.put(
                    {
                        "type": "translation_status",
                        "text": (
                            f"本机 Codex CLI 不支持 {rejected_model}，"
                            f"已自动切换为 {COMPATIBLE_TRANSLATION_MODEL} 并重试当前批次"
                        ),
                    }
                )
                try:
                    return self._app_server.translate(
                        quest,
                        prompt,
                        schema,
                        runtime,
                        cancel_event=cancel_event,
                        partial_callback=partial_callback,
                        preview_callback=preview_callback,
                    )
                except TranslationSuperseded:
                    raise
                except Exception as compatible_error:
                    self._app_server.close()
                    self._fast_mode_failed = True
                    self.events.put(
                        {
                            "type": "translation_status",
                            "text": (
                                "兼容模型常驻模式异常，当前批次自动改用兼容进程模式："
                                f"{compatible_error}"
                            ),
                        }
                    )
                    return self._invoke_exec(fallback_prompt or prompt)
            self._app_server.close()
            self._fast_mode_failed = True
            if self.stop_event.is_set():
                raise RuntimeError("AI 翻译已停止。") from fast_error
            self.events.put(
                {
                    "type": "translation_status",
                    "text": (
                        "预翻译流式服务异常，当前批次自动改用兼容模式："
                        if is_preload else
                        "极速常驻模式异常，当前批次自动改用兼容模式："
                    ) + str(fast_error),
                }
            )
            return self._invoke_exec(fallback_prompt or prompt, is_preload=is_preload)

    def run(self) -> None:
        if self.config.translation_engine == "app-server":
            try:
                self._app_server.start()
                _, _, profile_fast = self._profile(self.scope == "preload")
                self.events.put(
                    {
                        "type": "translation_status",
                        "text": (
                            "Codex 预翻译流式服务已预热"
                            if self.scope == "preload" else
                            "Codex 极速常驻服务已预热"
                        ) + ("（Fast 已开启）" if profile_fast else ""),
                    }
                )
            except Exception as exc:
                self._app_server.close()
                self._fast_mode_failed = True
                self.events.put(
                    {
                        "type": "translation_status",
                        "text": f"极速服务预热失败，将自动使用兼容模式：{exc}",
                    }
                )
        while not self.stop_event.is_set():
            self.wakeup.wait(0.5)
            if self.stop_event.is_set():
                break
            if not self.wakeup.is_set():
                continue
            # Background rows benefit from a short coalescing window.  The
            # current visible sentence does not: it is already a complete
            # SetText/AddText page and should enter Codex immediately.
            with self._lock:
                has_urgent = bool(self._urgent)
            if not has_urgent:
                self.stop_event.wait(self.config.translation_debounce_ms / 1000)
            batch = self._take_batch()
            if not batch:
                self.wakeup.clear()
                continue
            ids = [int(item["id"]) for item in batch]
            quest = str(batch[0]["quest"])
            is_preload = any(bool(item.get("preloaded")) for item in batch)
            profile_model, profile_reasoning, profile_fast = self._profile(is_preload)
            batch_detail = (
                (
                    f"整关单批 {len(ids)} 条（安全预算内）"
                    if len(ids) > 100 else
                    f"本批 {len(ids)} 条（大关卡回退上限 100）"
                )
                if is_preload else f"{len(ids)} 条"
            )
            self.store.mark_translating(ids)
            cancel_event = threading.Event()
            with self._lock:
                self._active_ids = set(ids)
                self._active_cancel = cancel_event
            self.events.put(
                {
                    "type": "translation_status",
                    "text": (
                        "AI Fast 正在翻译" if profile_fast else "AI 正在翻译"
                    )
                    + ("（完整剧情预载） " if is_preload else "（实时） ")
                    + f"{quest}：{batch_detail} · {profile_model}/{profile_reasoning}",
                    "ids": ids,
                }
            )
            try:
                partial_saved: dict[int, dict[str, Any]] = {}
                partial_source_ids: set[int] = set()
                last_preview_text = ""
                last_preview_at = 0.0

                def save_partial(value: dict[str, Any]) -> None:
                    """Persist each completed JSON object before the turn ends."""
                    try:
                        source_id = int(value.get("id", 0))
                        saved_now = self.store.save_translations([value], profile_model)
                    except (TypeError, ValueError, sqlite3.Error):
                        return
                    if not saved_now:
                        return
                    if source_id > 0:
                        partial_source_ids.add(source_id)
                    for saved_item in saved_now:
                        partial_saved[int(saved_item["id"])] = saved_item
                    self.events.put(
                        {
                            "type": "translation_partial",
                            "items": saved_now,
                            "model": profile_model,
                            "profile": "preload" if is_preload else "live",
                            "completed_count": len(partial_source_ids),
                            "requested_count": len(ids),
                        }
                    )

                def show_stream_preview(row_id: int, translation: str) -> None:
                    nonlocal last_preview_text, last_preview_at
                    if is_preload or row_id not in ids:
                        return
                    now = time.monotonic()
                    if translation == last_preview_text:
                        return
                    # Bound UI message volume while retaining the first token
                    # and smooth visible progress afterwards.
                    if last_preview_text and now - last_preview_at < 0.05:
                        return
                    last_preview_text = translation
                    last_preview_at = now
                    self.events.put(
                        {
                            "type": "translation_stream",
                            "id": row_id,
                            "translation": translation,
                            "model": profile_model,
                        }
                    )

                persistent = (
                    not is_preload
                    and self.config.translation_engine == "app-server"
                )
                keep_context = not (
                    persistent
                    and self._app_server.has_thread(quest)
                )
                prompt = self._build_prompt(
                    batch,
                    include_static_context=keep_context,
                    persistent_mode=persistent,
                )
                fallback_prompt = self._build_prompt(batch) if persistent else None
                result = self._invoke(
                    quest,
                    prompt,
                    fallback_prompt,
                    is_preload=is_preload,
                    cancel_event=cancel_event,
                    partial_callback=save_partial,
                    preview_callback=show_stream_preview,
                )
                translations = result.get("translations", [])
                if not isinstance(translations, list):
                    translations = []
                remaining = [
                    item for item in translations
                    if int(item.get("id", 0) or 0) not in partial_source_ids
                ]
                saved_by_id = dict(partial_saved)
                for saved_item in self.store.save_translations(remaining, profile_model):
                    saved_by_id[int(saved_item["id"])] = saved_item
                saved = list(saved_by_id.values())
                saved_ids = {int(item["id"]) for item in saved}
                missing = [row_id for row_id in ids if row_id not in saved_ids]
                if missing:
                    self.store.mark_translation_error(missing)
                memory = result.get("memory_updates", [])
                if isinstance(memory, list) and not is_preload:
                    self.store.save_quest_memory(quest, memory)
                styles = result.get("style_updates", [])
                if isinstance(styles, list):
                    self.store.save_speaker_styles(styles, set(ids))
                self.events.put(
                    {
                        "type": "translation_complete",
                        "items": saved,
                        "failed_ids": missing,
                        "model": profile_model,
                        "profile": "preload" if is_preload else "live",
                        "requested_count": len(ids),
                    }
                )
            except TranslationSuperseded:
                self.store.reset_translation_rows(ids)
                # The new current row is already urgent.  Put interrupted
                # history rows back behind it rather than losing them.
                self.enqueue(ids)
                self.events.put(
                    {
                        "type": "translation_preempted",
                        "ids": ids,
                        "text": "已抢占上一句，优先翻译当前画面",
                    }
                )
            except Exception as exc:
                unresolved = [
                    int(item["id"])
                    for item in self.store.translation_items(ids)
                    if not str(item.get("translation", ""))
                ]
                self.store.mark_translation_error(unresolved)
                self.events.put(
                    {
                        "type": "translation_error",
                        "ids": unresolved,
                        "message": str(exc),
                    }
                )
            finally:
                with self._lock:
                    if self._active_cancel is cancel_event:
                        self._active_cancel = None
                        self._active_ids.clear()
                    if not self._pending:
                        self.wakeup.clear()

    def close(self) -> None:
        self.stop_event.set()
        self.wakeup.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._app_server.close()
        self.join(timeout=3)


def render_explanation_result(result: dict[str, Any]) -> str:
    """Turn the structured explanation into calm, readable Chinese sections."""
    sections: list[str] = []
    for title, key in (
        ("这句话在说什么", "meaning"),
        ("以前发生过什么", "prior_context"),
        ("放在这段剧情里", "context"),
        ("语气与言外之意", "subtext"),
    ):
        value = normalize_text(str(result.get(key, "")))
        if value:
            sections.append(f"{title}\n{value}")
    notes = result.get("notes", [])
    if isinstance(notes, list):
        clean_notes = [normalize_text(str(item)) for item in notes]
        clean_notes = [item for item in clean_notes if item]
        if clean_notes:
            sections.append("需要留意\n" + "\n".join(f"• {item}" for item in clean_notes))
    sources = result.get("web_sources", [])
    if isinstance(sources, list):
        clean_sources: list[str] = []
        for source in sources[:4]:
            if not isinstance(source, dict):
                continue
            title = normalize_text(str(source.get("title", "")))
            summary = normalize_text(str(source.get("summary", "")))
            url = str(source.get("url", "")).strip()
            if not title or not re.match(r"^https?://", url, flags=re.IGNORECASE):
                continue
            block = f"• {title}"
            if summary:
                block += f"\n  {summary}"
            block += f"\n  {url}"
            clean_sources.append(block)
        if clean_sources:
            sections.append("联网参考\n" + "\n".join(clean_sources))
    uncertainty = normalize_text(str(result.get("uncertainty", "")))
    if uncertainty:
        sections.append(f"不确定之处\n{uncertainty}")
    return "\n\n".join(sections).strip()


class CodexExplanationWorker(threading.Thread):
    """Explain one line against the complete same-stage script without blocking UI."""

    def __init__(
        self,
        store: HistoryStore,
        config: RuntimeConfig,
        row_id: int,
        events: queue.Queue[dict[str, Any]],
    ):
        super().__init__(name=f"CodexExplanation-{row_id}", daemon=True)
        self.store = store
        self.config = config
        self.row_id = int(row_id)
        self.events = events
        self.process: subprocess.Popen[str] | None = None
        self.stop_event = threading.Event()

    def _prompt(self, payload: dict[str, Any]) -> str:
        if self.config.explanation_web_search:
            research_rules = (
                "目标句若含有仅凭当前关卡无法确认的旧剧情指代、人物别称、组织、事件、"
                "台词回收或多年以前的梗，必须先进行有针对性的实时联网搜索，再填写 "
                "prior_context 与 uncertainty。优先搜索日文原句、人物名和关卡名；必须打开"
                "实际页面，不得只依赖搜索摘要。资料优先级为 FGO 官方页面、fgo.wiki、"
                "Atlas Academy/官方数据镜像、可核对原文的成熟 FGO Wiki；关键身份尽量用"
                "两个来源交叉确认。为控制等待时间与 Token，最多进行 6 次检索、打开 4 个"
                "最相关页面；一旦两个独立来源已经足够确认就停止继续搜索，不读取无关的"
                "整站或超长剧情转储。web_sources 只列实际阅读并用于结论的页面，保留真实"
                "标题、URL 与一句话证据摘要。若搜索后只能得到高概率结论，明确写‘很可能’"
                "并在 uncertainty 说明依据差在哪里。\n"
            )
        else:
            research_rules = (
                "当前设置未开放联网搜索；只依据 scenario 作答，web_sources 返回空数组。\n"
            )
        return (
            "你是熟悉 Fate/Grand Order 世界观与日语表达的剧情解说者。"
            "只解释 target_id 对应的那一句，但必须先阅读同一关卡的完整 scenario。"
            "不要改写或续写剧情。\n"
            + research_rules
            +
            "说明规则：\n"
            "1. meaning 用通俗简体中文直接说明这句话的字面意思；\n"
            "2. prior_context 说明理解本句所需的更早剧情、旧活动或人物关系；没有则留空；\n"
            "3. context 结合当前关卡前因后果说明说话者为什么此时这样说；\n"
            "4. subtext 解释语气、隐含态度、代词指向、双关或省略内容；\n"
            "5. notes 只列理解这句话必要的人名、术语和文化背景；\n"
            "6. uncertainty 只保留查阅当前剧情与可用资料后仍无法确认的部分，不能编造；\n"
            "7. scenario 可能包含目标句之后尚未播放的内容，只可用于消歧，"
            "解释中不要主动泄露后续事件；也不剧透 scenario 之外的无关内容；\n"
            "8. 最终只输出 Schema JSON。\n\n"
            "输入：\n" + json.dumps(payload, ensure_ascii=False)
        )

    def _command(self, schema: Path, output: Path, runtime: Path) -> list[str]:
        command = [*_codex_command(self.config.codex_path)]
        # --search is a global Codex flag and therefore precedes `exec`.
        # The model receives native live search while local files remain read-only.
        if self.config.explanation_web_search:
            command.append("--search")
        command.extend(
            [
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-m",
                self.config.preload_translation_model,
                "-c",
                f'model_reasoning_effort="{self.config.preload_translation_reasoning}"',
            ]
        )
        if fast_mode_effective(self.config, preloaded=True):
            command.extend(["-c", 'service_tier="priority"'])
        command.extend(
            [
                "--output-schema", str(schema),
                "-o", str(output),
                "-C", str(runtime),
                "-",
            ]
        )
        return command

    def run(self) -> None:
        try:
            payload = self.store.explanation_context(self.row_id)
            if not payload:
                raise RuntimeError("找不到这条剧情记录，可能已经被清除。")
            self.events.put(
                {
                    "type": "explanation_started",
                    "row_id": self.row_id,
                    "quest": str(payload.get("quest", "")),
                    "line_count": len(payload.get("scenario", [])),
                    "web_search": self.config.explanation_web_search,
                }
            )
            runtime = data_dir() / "translation-runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            output = runtime / f"explanation-{uuid.uuid4().hex}.json"
            schema = resource_path("translation_runtime/explanation_schema.json")
            if not schema.is_file():
                raise RuntimeError(f"解释输出结构文件缺失：{schema}")
            command = self._command(schema, output, runtime)
            if self.stop_event.is_set():
                return
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                self.process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                stdout, _ = self.process.communicate(self._prompt(payload), timeout=600)
                if self.stop_event.is_set():
                    return
                if self.process.returncode != 0:
                    tail = "\n".join(stdout.strip().splitlines()[-8:])
                    raise RuntimeError(f"Codex 解释失败（{self.process.returncode}）：{tail}")
                if not output.is_file():
                    raise RuntimeError("Codex 没有生成解释结果。")
                result = json.loads(output.read_text(encoding="utf-8-sig"))
                if not isinstance(result, dict):
                    raise RuntimeError("解释结果格式不正确。")
                explanation = render_explanation_result(result)
                if not explanation:
                    raise RuntimeError("Codex 返回了空解释。")
                self.store.save_explanation(
                    self.row_id,
                    explanation,
                    self.config.preload_translation_model,
                    web_enabled=self.config.explanation_web_search,
                )
                self.events.put(
                    {
                        "type": "explanation_complete",
                        "row_id": self.row_id,
                        "quest": str(payload.get("quest", "")),
                        "explanation": explanation,
                        "model": self.config.preload_translation_model,
                        "web_search": self.config.explanation_web_search,
                        "source_count": len(result.get("web_sources", []))
                        if isinstance(result.get("web_sources"), list) else 0,
                    }
                )
            finally:
                self.process = None
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:
            if not self.stop_event.is_set():
                self.events.put(
                    {
                        "type": "explanation_error",
                        "row_id": self.row_id,
                        "message": str(exc),
                    }
                )

    def close(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3)


class JapaneseTextResolver:
    """Legacy deterministic resolver retained only for old-data repair tests."""

    def __init__(self):
        self.seed = dict(SEED_CHARACTER_MAP)
        self.votes_path = data_dir() / "character_votes.json"
        self._lock = threading.RLock()
        self.votes: dict[str, Counter[str]] = defaultdict(Counter)
        try:
            saved = json.loads(self.votes_path.read_text(encoding="utf-8"))
            for encoded, choices in saved.items():
                if isinstance(choices, dict):
                    self.votes[str(encoded)].update(
                        {str(k): int(v) for k, v in choices.items() if int(v) > 0}
                    )
        except Exception:
            pass

    def _learned(self, encoded: str) -> str | None:
        if encoded in self.seed:
            return self.seed[encoded]
        choices = self.votes.get(encoded)
        if not choices:
            return None
        ranked = choices.most_common(2)
        best, count = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        if count >= 2 and count >= runner_up * 2:
            return best
        return None

    def decode_known(self, raw: str) -> str:
        visible = strip_display_tags(raw)
        if not probably_encoded_text(visible):
            return normalize_text(visible)
        with self._lock:
            return "".join(self._learned(ch) or ch for ch in visible)

    @staticmethod
    def _line_bounds(line: dict[str, Any]) -> tuple[float, float, float, float]:
        words = line.get("words") or []
        if not words:
            return 0.0, 0.0, 0.0, 0.0
        x1 = min(float(w.get("x", 0)) for w in words)
        y1 = min(float(w.get("y", 0)) for w in words)
        x2 = max(float(w.get("x", 0)) + float(w.get("width", 0)) for w in words)
        y2 = max(float(w.get("y", 0)) + float(w.get("height", 0)) for w in words)
        return x1, y1, x2, y2

    def extract_story(self, ocr: dict[str, Any]) -> tuple[str, str]:
        width = max(1.0, float(ocr.get("width", 1)))
        height = max(1.0, float(ocr.get("height", 1)))
        candidates: list[tuple[float, float, str]] = []
        ignored = {"SKIP", "LOG", "AUTO", "WAVE", "BATTLE", "TURN"}
        for line in ocr.get("lines") or []:
            x1, y1, x2, y2 = self._line_bounds(line)
            text = compact_ocr_text(str(line.get("text", "")))
            if not text or any(token in text.upper() for token in ignored):
                continue
            if x1 > width * 0.88 or y2 < height * 0.62:
                continue
            if not contains_japanese(text):
                continue
            candidates.append((y1 / height, x1 / width, text))

        # Standard, face and most event message windows put the speaker above
        # the body. Keeping the bounds relative also works at other resolutions.
        name_lines = [x for x in candidates if 0.62 <= x[0] < 0.78 and x[1] < 0.45]
        body_lines = [x for x in candidates if x[0] >= 0.75 and x[1] < 0.82]
        speaker = name_lines[-1][2] if name_lines else ""
        body = "".join(x[2] for x in sorted(body_lines))
        # The decorative name-plate edge is often recognized as punctuation.
        speaker = re.sub(r"[-=・.。]+$", "", speaker)
        body = re.sub(r"(?:[0〇]|[|｜])$", "", body)
        return sanitize_speaker(speaker), body

    def extract_choices(
        self,
        ocr: dict[str, Any],
        expected_lengths: list[int],
    ) -> list[str]:
        """Pick the vertically stacked, centered choice labels from full-screen OCR."""
        width = max(1.0, float(ocr.get("width", 1)))
        height = max(1.0, float(ocr.get("height", 1)))
        ignored = {"SKIP", "LOG", "AUTO", "WAVE", "BATTLE", "TURN", "SELECT"}
        candidates: list[dict[str, float | str]] = []
        for line in ocr.get("lines") or []:
            x1, y1, x2, y2 = self._line_bounds(line)
            text = compact_ocr_text(str(line.get("text", "")))
            if not text or any(token in text.upper() for token in ignored):
                continue
            if not contains_japanese(text):
                continue
            yn = y1 / height
            if not 0.10 <= yn <= 0.76 or x1 > width * 0.90:
                continue
            candidates.append(
                {
                    "text": text,
                    "x1": x1 / width,
                    "x2": x2 / width,
                    "y": yn,
                    "y2": y2 / height,
                }
            )
        candidates.sort(key=lambda item: float(item["y"]))

        # OCR can split a long option into two lines. Lines much closer than a
        # normal choice-button pitch are joined before selecting the stack.
        grouped: list[dict[str, float | str]] = []
        for item in candidates:
            if grouped:
                previous = grouped[-1]
                center = (float(item["x1"]) + float(item["x2"])) / 2
                old_center = (float(previous["x1"]) + float(previous["x2"])) / 2
                if (
                    float(item["y"]) - float(previous["y"]) < 0.045
                    and abs(center - old_center) < 0.20
                ):
                    previous["text"] = str(previous["text"]) + str(item["text"])
                    previous["x1"] = min(float(previous["x1"]), float(item["x1"]))
                    previous["x2"] = max(float(previous["x2"]), float(item["x2"]))
                    previous["y2"] = float(item["y2"])
                    continue
            grouped.append(dict(item))

        count = len(expected_lengths)
        if count <= 0:
            return [str(item["text"]) for item in grouped]
        if len(grouped) <= count:
            return [str(item["text"]) for item in grouped]

        # Choice labels share a center line and roughly match the managed
        # string lengths. Evaluate combinations because background Japanese
        # text can remain visible behind the dialog.
        usable = grouped[:18]
        best: tuple[float, tuple[int, ...]] | None = None
        for indexes in itertools.combinations(range(len(usable)), count):
            selected = [usable[index] for index in indexes]
            centers = [
                (float(item["x1"]) + float(item["x2"])) / 2 for item in selected
            ]
            lengths = [len(compact_ocr_text(str(item["text"]))) for item in selected]
            score = sum(abs(center - 0.5) for center in centers)
            score += (max(centers) - min(centers)) * 2.0
            score += sum(
                abs(actual - expected) / max(1, expected)
                for actual, expected in zip(lengths, expected_lengths)
            )
            ys = [float(item["y"]) for item in selected]
            score += sum(1.5 for left, right in zip(ys, ys[1:]) if right - left < 0.035)
            candidate = (score, indexes)
            if best is None or candidate < best:
                best = candidate
        if best is None:
            return []
        return [str(usable[index]["text"]) for index in best[1]]

    def resolve_choices(self, raw_choices: list[str], ocr: dict[str, Any]) -> list[str]:
        expected_lengths = [
            len(compact_ocr_text(strip_display_tags(value))) for value in raw_choices
        ]
        observed = self.extract_choices(ocr, expected_lengths)
        return [
            self.resolve(raw, observed[index] if index < len(observed) else "")
            for index, raw in enumerate(raw_choices)
        ]

    def _align(self, raw_chars: list[str], observed: str) -> list[str | None]:
        """Align OCR characters to encoded positions, using known pairs as anchors."""
        obs = list(compact_ocr_text(observed))
        n, m = len(raw_chars), len(obs)
        if not n:
            return []
        # Dynamic programming is tiny for dialogue pages (normally < 100 chars).
        dp = [[0.0] * (m + 1) for _ in range(n + 1)]
        back = [[0] * (m + 1) for _ in range(n + 1)]  # 0 diag, 1 delete, 2 insert
        for i in range(1, n + 1):
            dp[i][0] = float(i)
            back[i][0] = 1
        for j in range(1, m + 1):
            dp[0][j] = float(j)
            back[0][j] = 2
        for i in range(1, n + 1):
            expected = self._learned(raw_chars[i - 1])
            for j in range(1, m + 1):
                if expected is None:
                    substitution = 0.15
                else:
                    substitution = 0.0 if expected == obs[j - 1] else 2.25
                options = (
                    (dp[i - 1][j - 1] + substitution, 0),
                    (dp[i - 1][j] + 1.0, 1),
                    (dp[i][j - 1] + 1.0, 2),
                )
                dp[i][j], back[i][j] = min(options, key=lambda x: x[0])
        aligned: list[str | None] = [None] * n
        i, j = n, m
        while i or j:
            move = back[i][j]
            if i and j and move == 0:
                aligned[i - 1] = obs[j - 1]
                i -= 1
                j -= 1
            elif i and (not j or move == 1):
                i -= 1
            else:
                j -= 1
        return aligned

    def resolve(self, raw: str, observed: str) -> str:
        visible = strip_display_tags(raw)
        raw_compare = comparison_text(visible)
        observed_compare = comparison_text(observed)
        if raw_compare:
            similarity = (
                SequenceMatcher(None, raw_compare, observed_compare).ratio()
                if observed_compare else 0.0
            )
            # OCR is used only as proof that the managed string is already the
            # rendered text. Return the managed string itself so punctuation,
            # line breaks and glyphs are not degraded by OCR.
            if similarity >= 0.72 or not probably_encoded_text(visible):
                return normalize_text(visible)
        chars = [ch for ch in visible if ch != "\n"]
        aligned = self._align(chars, observed) if observed else [None] * len(chars)
        observed_length = len(compact_ocr_text(observed))
        anchors = 0
        anchor_matches = 0
        for encoded, seen in zip(chars, aligned):
            expected = self._learned(encoded)
            if expected and seen:
                anchors += 1
                anchor_matches += int(expected == seen)
        implausible_length = bool(chars) and not (
            len(chars) * 0.55 <= observed_length <= len(chars) * 1.45
        )
        if implausible_length or (anchors >= 3 and anchor_matches / anchors < 0.55):
            # Usually means the framebuffer already moved to another page.
            # Never poison the learned map with a mismatched screenshot.
            aligned = [None] * len(chars)
        output: list[str] = []
        pairs: list[tuple[str, str]] = []
        index = 0
        with self._lock:
            for ch in visible:
                if ch == "\n":
                    output.append("\n")
                    continue
                known = self._learned(ch)
                seen = aligned[index] if index < len(aligned) else None
                output.append(known or seen or ch)
                if seen and ch not in self.seed and contains_japanese(seen):
                    pairs.append((ch, seen))
                index += 1
            changed = False
            for encoded, decoded in pairs:
                self.votes[encoded][decoded] += 1
                changed = True
            if changed:
                try:
                    serializable = {k: dict(v) for k, v in self.votes.items()}
                    self.votes_path.write_text(
                        json.dumps(serializable, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
        return normalize_text("".join(output))


class BridgeError(RuntimeError):
    pass


class EmulatorBridge:
    def __init__(self, config: RuntimeConfig, notify: Callable[[dict[str, Any]], None]):
        self.config = config
        self.notify = notify
        self.adb = self._find_adb()
        self.serial = config.serial
        self.server_session = None
        self.gadget_session = None
        self.hook_script = None
        self.detached_event = threading.Event()

    def _find_adb(self) -> Path:
        choices = [
            self.config.adb_path,
            os.environ.get("FGO_ADB", ""),
            r"E:\leidian\LDPlayer9\adb.exe",
            r"C:\leidian\LDPlayer9\adb.exe",
            r"D:\leidian\LDPlayer9\adb.exe",
        ]
        found = shutil.which("adb")
        if found:
            choices.append(found)
        for value in choices:
            if value and Path(value).is_file():
                self.config.adb_path = str(Path(value))
                self.config.save()
                return Path(value)
        raise BridgeError("未找到雷电 ADB。可在 %LOCALAPPDATA%\\FgoStoryListener\\config.json 设置 adb_path。")

    def _run(self, args: list[str], timeout: int = 30, check: bool = True) -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(
            [str(self.adb), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
        )
        output = proc.stdout.strip()
        if check and proc.returncode != 0:
            raise BridgeError(f"ADB 命令失败：{' '.join(args)}\n{output}")
        return output

    def adb_device(self, args: list[str], timeout: int = 30, check: bool = True) -> str:
        return self._run(["-s", self.serial, *args], timeout=timeout, check=check)

    def _repair_ldplayer_forward(self) -> None:
        candidates = [
            Path(r"C:\Program Files\ldplayer9box\VBoxManage.exe"),
            Path(r"C:\Program Files\dnplayerext2\VBoxManage.exe"),
        ]
        manager = next((p for p in candidates if p.is_file()), None)
        if not manager:
            return
        vm = f"leidian{self.config.emulator_index}"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.run(
            [str(manager), "controlvm", vm, "natpf1", "delete", "adb"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            timeout=10,
        )
        host_port = self.serial.rsplit(":", 1)[-1]
        subprocess.run(
            [
                str(manager), "controlvm", vm, "natpf1",
                f"adb,tcp,127.0.0.1,{host_port},,5555",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            timeout=10,
        )

    def connect_adb(self) -> None:
        self.notify({"type": "status", "text": "正在连接雷电模拟器…"})
        result = self._run(["connect", self.serial], check=False)
        if "connected" not in result.lower() and "already" not in result.lower():
            self._repair_ldplayer_forward()
            time.sleep(1)
            result = self._run(["connect", self.serial], check=False)
        state = self.adb_device(["get-state"], check=False)
        if "device" not in state:
            raise BridgeError(f"无法连接雷电模拟器 {self.serial}：{result or state}")

        root_result = self.adb_device(["root"], check=False)
        if "restarting" in root_result.lower():
            time.sleep(2)
            self._run(["connect", self.serial], check=False)
        uid = self.adb_device(["shell", "id", "-u"], check=False).strip()
        if uid != "0":
            raise BridgeError("雷电模拟器尚未开启 root。请在雷电设置 → 其他 → Root 权限中开启。")

    def _remote_size(self, path: str) -> int:
        out = self.adb_device(["shell", "stat", "-c", "%s", path], check=False)
        try:
            return int(out.splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    def _push_if_needed(self, local: Path, remote: str, mode: str) -> None:
        if not local.is_file():
            raise BridgeError(f"运行文件缺失：{local}")
        if self._remote_size(remote) != local.stat().st_size:
            self.adb_device(["push", str(local), remote], timeout=180)
        self.adb_device(["shell", "chmod", mode, remote])

    def _wait_pid(self, seconds: int = 30) -> int:
        deadline = time.time() + seconds
        while time.time() < deadline:
            value = self.adb_device(["shell", "pidof", PACKAGE], check=False).strip()
            if value:
                try:
                    return int(value.split()[0])
                except ValueError:
                    pass
            time.sleep(1)
        raise BridgeError("FGO 尚未运行。请先在雷电模拟器中启动游戏。")

    def prepare(self) -> int:
        self.connect_adb()
        runtime = resource_path("runtime")
        server = runtime / "frida-server-17.17.0-android-x86_64"
        gadget = runtime / "frida-gadget-17.17.0-android-arm64.so"
        remote_server = "/data/local/tmp/.fgo-story-bridge"
        remote_gadget = "/data/local/tmp/libfgo-story-listener.so"

        self.notify({"type": "status", "text": "正在部署监听运行时…"})
        self._push_if_needed(server, remote_server, "755")
        self._push_if_needed(gadget, remote_gadget, "755")

        config = json.dumps(
            {
                "interaction": {
                    "type": "listen",
                    "address": "127.0.0.1",
                    "port": GADGET_PORT,
                    "on_port_conflict": "fail",
                    "on_load": "resume",
                },
                "runtime": "qjs",
            },
            separators=(",", ":"),
        )
        local_config = data_dir() / "gadget.config"
        local_config.write_text(config, encoding="utf-8")
        self.adb_device(
            ["push", str(local_config), "/data/local/tmp/libfgo-story-listener.config"]
        )
        self.adb_device(
            ["shell", "chmod", "644", "/data/local/tmp/libfgo-story-listener.config"]
        )

        # Start the x86_64 control server used only to invoke Houdini's
        # NativeBridgeLoadLibraryExt. It does not hook IL2CPP itself.
        self.adb_device(
            [
                "shell",
                "pidof .fgo-story-bridge >/dev/null 2>&1 || "
                "nohup /data/local/tmp/.fgo-story-bridge -l 127.0.0.1:27042 "
                ">/data/local/tmp/.fgo-story-bridge.log 2>&1 &",
            ],
            check=False,
        )
        time.sleep(1)
        self.adb_device(["forward", f"tcp:{SERVER_PORT}", f"tcp:{SERVER_PORT}"])
        self.adb_device(["forward", f"tcp:{GADGET_PORT}", f"tcp:{GADGET_PORT}"])

        # Place Gadget in the package's active arm64 native library directory.
        # Houdini v3 rejects arbitrary /data/local/tmp library paths.
        code_path = self.adb_device(["shell", "pm", "path", PACKAGE], check=False)
        base_line = next((x for x in code_path.splitlines() if x.startswith("package:") and x.endswith("base.apk")), "")
        if not base_line:
            raise BridgeError("雷电模拟器中未找到 com.aniplex.fategrandorder。")
        app_dir = str(Path(base_line.removeprefix("package:")).parent).replace("\\", "/")
        lib_dir = f"{app_dir}/lib/arm64"
        installed_gadget = f"{lib_dir}/libfgo-story-listener.so"
        remote_config = f"{lib_dir}/libfgo-story-listener.config"
        self.adb_device(
            [
                "shell",
                f"cp {remote_gadget} {installed_gadget} && "
                f"cp /data/local/tmp/libfgo-story-listener.config {remote_config} && "
                f"cp /data/local/tmp/libfgo-story-listener.config {remote_config}.so && "
                f"chown system:system {lib_dir}/libfgo-story-listener* && "
                f"chmod 755 {installed_gadget} && chmod 644 {remote_config} {remote_config}.so && "
                f"(restorecon {lib_dir}/libfgo-story-listener* 2>/dev/null || true)",
            ],
            timeout=90,
        )
        pid = self._wait_pid()
        self._load_gadget(pid, installed_gadget)
        return pid

    def _gadget_available(self) -> bool:
        result: queue.Queue[tuple[bool, Exception | None]] = queue.Queue(maxsize=1)

        def probe() -> None:
            try:
                device = frida.get_device_manager().add_remote_device(
                    f"127.0.0.1:{GADGET_PORT}"
                )
                result.put((bool(device.enumerate_processes()), None))
            except Exception as exc:
                result.put((False, exc))

        threading.Thread(target=probe, name="FridaGadgetProbe", daemon=True).start()
        try:
            available, error = result.get(timeout=8)
        except queue.Empty as exc:
            raise BridgeError(
                "IL2CPP Hook 端口 8 秒无响应。请完全关闭并重新启动 FGO，再点“重新连接”。"
            ) from exc
        if error is not None:
            return False
        return available

    def _load_gadget(self, pid: int, library_path: str) -> None:
        if self._gadget_available():
            return
        self.notify({"type": "status", "text": "正在载入 arm64 IL2CPP Hook…"})
        manager = frida.get_device_manager()
        device = manager.add_remote_device(f"127.0.0.1:{SERVER_PORT}")
        session = device.attach(pid)
        result: queue.Queue[dict[str, Any]] = queue.Queue()
        script_source = r"""
        'use strict';
        rpc.exports = {
          load: function (path) {
            const houdini = Process.getModuleByName('libhoudini.so');
            const item = houdini.enumerateExports().find(function (x) {
              return x.name === 'NativeBridgeItf';
            });
            if (!item) throw new Error('NativeBridgeItf not found');
            const table = item.address;
            const version = table.readU32();
            if (version < 3) throw new Error('NativeBridge v3 required, got ' + version);
            const loadExt = new NativeFunction(
              table.add(112).readPointer(), 'pointer', ['pointer', 'int', 'pointer']
            );
            const handle = loadExt(Memory.allocUtf8String(path), 2, ptr(0));
            if (handle.isNull()) throw new Error('NativeBridgeLoadLibraryExt returned null');
            return handle.toString();
          }
        };
        """
        script = session.create_script(script_source)
        script.load()
        try:
            script.exports_sync.load(library_path)
        finally:
            try:
                script.unload()
            except Exception:
                pass
            session.detach()
        deadline = time.time() + 12
        while time.time() < deadline:
            if self._gadget_available():
                return
            time.sleep(0.5)
        raise BridgeError("arm64 Gadget 已载入，但监听端口未就绪。")

    def attach_hook(self) -> None:
        manager = frida.get_device_manager()
        device = manager.add_remote_device(f"127.0.0.1:{GADGET_PORT}")
        processes = device.enumerate_processes()
        if not processes:
            raise BridgeError("Gadget 进程不可用。")
        self.gadget_session = device.attach(processes[0].pid)
        def on_detached(reason: Any, crash: Any) -> None:
            self.detached_event.set()
            self.notify(
                {"type": "detached", "reason": str(reason), "crash": str(crash or "")}
            )

        self.gadget_session.on("detached", on_detached)
        source = resource_path("hook.js").read_text(encoding="utf-8-sig")
        self.hook_script = self.gadget_session.create_script(source)
        self.hook_script.on("message", self._on_frida_message)
        self.hook_script.load()

    def _on_frida_message(self, message: dict[str, Any], data: bytes | None) -> None:
        if message.get("type") == "send" and isinstance(message.get("payload"), dict):
            self.notify(message["payload"])
        elif message.get("type") == "error":
            self.notify(
                {
                    "type": "diagnostic",
                    "level": "error",
                    "where": "Frida",
                    "message": message.get("description", "hook script error"),
                    "stack": message.get("stack", ""),
                }
            )

    def close(self) -> None:
        self.detached_event.set()
        if self.hook_script is not None:
            try:
                self.hook_script.unload()
            except Exception:
                pass
        if self.gadget_session is not None:
            try:
                self.gadget_session.detach()
            except Exception:
                pass


class DialogueProcessor:
    """Normalize trusted IL2CPP text without taking screenshots or using OCR."""

    def __init__(
        self,
        bridge: EmulatorBridge,
        output: queue.Queue[dict[str, Any]],
        stop_event: threading.Event,
    ):
        self.bridge = bridge
        self.output = output
        self.stop_event = stop_event
        self.jobs: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="FgoTextResolver", daemon=True)
        self._uncertain_reported = False
        self._active_choices: dict[str, list[str]] = {}
        self.thread.start()

    def notify(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in {
            "dialogue", "dialogue_preview", "choices", "choice_selected", "scenario_plan"
        }:
            self.jobs.put(dict(event))
            return
        self.output.put(event)

    def _report_uncertain_once(self, source: str) -> None:
        if self._uncertain_reported:
            return
        self._uncertain_reported = True
        self.output.put(
            {
                "type": "diagnostic",
                "level": "debug",
                "where": "ManagedTextOnly",
                "message": (
                    f"已忽略无法确定的实时片段（{source}），等待完整剧情脚本或游戏 LOG 补齐；"
                    "未调用截图 OCR。"
                ),
            }
        )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                event = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                kind = str(event.get("type", ""))
                if kind == "scenario_plan":
                    entries: list[dict[str, str]] = []
                    for value in event.get("entries", [])[:4096]:
                        if not isinstance(value, dict):
                            continue
                        # Only empty-tag visible text slots emitted by the
                        # hook are eligible. Command parameters never receive
                        # this kind.
                        if str(value.get("kind", "")) != "dialogue_fragment":
                            continue
                        raw_text = str(value.get("text", ""))
                        raw_speaker = str(value.get("speaker", ""))
                        if not managed_text_is_lossless(raw_text):
                            continue
                        if raw_speaker and not managed_text_is_lossless(raw_speaker):
                            raw_speaker = ""
                        entries.append(
                            {
                                "speaker": sanitize_speaker(normalize_text(raw_speaker)),
                                "text": normalize_text(raw_text),
                            }
                        )
                    if entries:
                        resolved = dict(event)
                        resolved["entries"] = entries
                        resolved["source"] = f"{event.get('source', '')}+DirectManagedText"
                        self.output.put(resolved)
                    continue
                if kind == "choice_selected":
                    dialog_id = str(event.get("dialog_id", "global"))
                    index = int(event.get("index", -1) or 0)
                    raw_text = str(event.get("text", ""))
                    cached = self._active_choices.get(dialog_id, [])
                    selected = (
                        cached[index]
                        if 0 <= index < len(cached)
                        else normalize_text(raw_text)
                    )
                    if not selected or not managed_text_is_lossless(raw_text):
                        self._report_uncertain_once(str(event.get("source", "choice_selected")))
                        continue
                    resolved = dict(event)
                    resolved.update({"text": selected, "raw_text": raw_text})
                    self.output.put(resolved)
                    continue
                if kind == "choices":
                    raw_choices = [str(value) for value in event.get("choices", [])]
                    if not raw_choices or not all(
                        managed_text_is_lossless(value) for value in raw_choices
                    ):
                        self._report_uncertain_once(str(event.get("source", "choices")))
                        continue
                    choices = [normalize_text(value) for value in raw_choices]
                    resolution_source = f"{event.get('source', '')}+DirectManagedText"
                    dialog_id = str(event.get("dialog_id", "global"))
                    self._active_choices[dialog_id] = choices
                    resolved = dict(event)
                    resolved.update(
                        {
                            "choices": choices,
                            "raw_choices": raw_choices,
                            "source": resolution_source,
                        }
                    )
                    self.output.put(resolved)
                    continue
                if kind == "dialogue" and event.get("source") == "CurrentMessageSnapshot":
                    raw_speaker = str(event.get("speaker", ""))
                    raw_text = str(event.get("text", ""))
                    if not raw_text or probably_encoded_text(raw_text):
                        self._report_uncertain_once("CurrentMessageSnapshot")
                        continue
                    resolved = dict(event)
                    resolved.update(
                        {
                            "speaker": sanitize_speaker(raw_speaker),
                            "text": normalize_text(raw_text),
                            "raw_speaker": raw_speaker,
                            "raw_text": raw_text,
                            "source": "CurrentMessageSnapshot+RenderedText",
                        }
                    )
                    self.output.put(resolved)
                    continue
                # CurrentMessageSnapshot is handled and continued above. Every
                # remaining dialogue event here is a live managed-text event;
                # no separate is_snapshot local exists or is needed.
                if kind in {"dialogue", "dialogue_preview"}:
                    raw_speaker = str(event.get("speaker", ""))
                    raw_text = str(event.get("text", ""))
                    if managed_text_is_lossless(raw_text) and (
                        not raw_speaker or managed_text_is_lossless(raw_speaker)
                    ):
                        resolved = dict(event)
                        resolved.update(
                            {
                                "speaker": sanitize_speaker(normalize_text(raw_speaker)),
                                "text": normalize_text(raw_text),
                                "raw_speaker": raw_speaker,
                                "raw_text": raw_text,
                                "source": f"{event.get('source', '')}+DirectManagedText",
                            }
                        )
                        self.output.put(resolved)
                        continue
                # Never guess from a framebuffer.  The same page will be
                # recovered losslessly from ScriptBackLog.logData, or it is
                # already present in the parsed scenario plan.
                self._report_uncertain_once(str(event.get("source", kind)))
            except Exception as exc:
                self.output.put(
                    {
                        "type": "diagnostic",
                        "level": "warning",
                        "where": "ManagedTextProcessor",
                        "message": str(exc),
                    }
                )

    def close(self) -> None:
        self.jobs.put(None)
        self.thread.join(timeout=2)


class ListenerThread(threading.Thread):
    def __init__(self, config: RuntimeConfig, events: queue.Queue[dict[str, Any]]):
        super().__init__(name="FgoListener", daemon=True)
        self.config = config
        self.events = events
        self.stop_event = threading.Event()
        self.bridge: EmulatorBridge | None = None
        self.processor: DialogueProcessor | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                # Once ADB is available, dialogue events are normalized from
                # trusted managed strings off Frida's callback thread. No
                # framebuffer capture or OCR is performed.
                self.bridge = EmulatorBridge(self.config, self.events.put)
                self.bridge.prepare()
                self.processor = DialogueProcessor(self.bridge, self.events, self.stop_event)
                self.bridge.notify = self.processor.notify
                self.bridge.attach_hook()
                self.events.put({"type": "status", "text": "监听中：请在 FGO 内打开剧情", "ready": True})
                while not self.stop_event.wait(0.5):
                    if self.bridge.detached_event.is_set():
                        break
            except Exception as exc:
                self.events.put(
                    {
                        "type": "worker_error",
                        "message": str(exc),
                        "stack": traceback.format_exc(),
                    }
                )
                if self.stop_event.wait(5):
                    break
            finally:
                if self.processor:
                    self.processor.close()
                    self.processor = None
                if self.bridge:
                    self.bridge.close()
                    self.bridge = None
            if not self.stop_event.is_set():
                self.events.put({"type": "status", "text": "等待 FGO 重新启动…"})
                self.stop_event.wait(3)

    def stop(self) -> None:
        self.stop_event.set()
        if self.processor:
            self.processor.close()
        if self.bridge:
            self.bridge.close()


class BilingualOverlay(tk.Toplevel):
    """Focused current-line reader at normal Windows z-order."""

    def __init__(self, owner: "Application"):
        super().__init__(owner)
        self.owner = owner
        self.title("FGO 当前译文")
        self.configure(bg=UI_BG)
        self.attributes("-topmost", False)
        self.geometry("960x380")
        self.minsize(620, 330)
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self.card = tk.Frame(
            self, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        self.card.pack(fill="both", expand=True, padx=16, pady=16)

        self.header = tk.Frame(self.card, bg=UI_CARD)
        self.header.pack(fill="x", padx=20, pady=(16, 8))
        self.quest_var = tk.StringVar(value="等待剧情文本…")
        tk.Label(
            self.header,
            textvariable=self.quest_var,
            bg=UI_CARD,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self.log_button = apple_button(
            self.header, "查看 LOG", owner.show_log, compact=True
        )
        self.log_button.pack(side="right", padx=(8, 0))
        self.explain_button = apple_button(
            self.header, "解释这句", owner.explain_current, primary=True, compact=True
        )
        self.explain_button.configure(state="disabled")
        self.explain_button.pack(side="right")

        self.speaker_var = tk.StringVar(value="")
        self.speaker_label = tk.Label(
            self.card,
            textvariable=self.speaker_var,
            bg=UI_CARD,
            fg=UI_BLUE,
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="nw",
            justify="left",
        )
        self.speaker_label.pack(fill="x", padx=20)

        self.source_var = tk.StringVar(value="")
        self.source_label = tk.Label(
            self.card,
            textvariable=self.source_var,
            bg=UI_CARD,
            fg=UI_JAPANESE,
            font=("Yu Gothic UI", 11),
            anchor="nw",
            justify="left",
            wraplength=800,
        )
        self.source_label.pack(fill="x", padx=20, pady=(4, 9))

        tk.Frame(self.card, bg=UI_DIVIDER, height=1).pack(fill="x", padx=20)
        self.translation_var = tk.StringVar(value="翻译完成后会自动显示在这里")
        self.translation_label = tk.Label(
            self.card,
            textvariable=self.translation_var,
            bg=UI_CARD,
            fg=UI_TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="nw",
            justify="left",
            wraplength=800,
        )
        self.translation_label.pack(fill="x", padx=20, pady=(11, 16))
        self.spacer = tk.Frame(self.card, bg=UI_CARD)
        self.spacer.pack(fill="both", expand=True)
        self._wrap_job: str | None = None
        self.bind("<Configure>", self._resize_wrap)

    def _resize_wrap(self, event: tk.Event) -> None:
        if getattr(event, "widget", self) is not self:
            return
        if self._wrap_job is not None:
            try:
                self.after_cancel(self._wrap_job)
            except tk.TclError:
                pass
        self._wrap_job = self.after_idle(self._apply_wrap)

    def _apply_wrap(self) -> None:
        self._wrap_job = None
        # Use the actual content card width. The former root-width estimate was
        # wider than the Label after its padding was applied, so Tk laid text
        # out as one long line and then clipped the right-hand characters.
        width = max(260, self.card.winfo_width() - 42)
        self.source_label.configure(wraplength=width)
        self.translation_label.configure(wraplength=width)
        self.update_idletasks()
        self._fit_content_height()

    def _fit_content_height(self) -> None:
        if self.state() == "withdrawn":
            return
        self.update_idletasks()
        required = self.card.winfo_reqheight() + 32
        maximum = max(360, self.winfo_screenheight() - 140)
        desired = max(330, min(maximum, required))
        self.minsize(620, desired)
        if self.winfo_height() < desired:
            self.geometry(f"{max(620, self.winfo_width())}x{desired}")

    def show_line(
        self, quest: str, speaker: str, source: str, translation: str
    ) -> None:
        self.quest_var.set(quest)
        self.speaker_var.set(self.owner.display_speaker(quest, speaker))
        self.source_var.set(normalize_text(source))
        self.translation_var.set(normalize_text(translation) or "正在翻译…")
        self.explain_button.configure(state="normal")
        self.after_idle(self._apply_wrap)

    def show_preview(self, quest: str, speaker: str, source: str) -> None:
        """Paint Hook text immediately without creating a history record."""
        self.quest_var.set(quest)
        self.speaker_var.set(self.owner.display_speaker(quest, speaker))
        self.source_var.set(normalize_text(source))
        self.translation_var.set("正在匹配预翻译…")
        # Until the 220 ms authoritative event arrives, explain_current still
        # points at the previous database row.
        self.explain_button.configure(state="disabled")
        self.after_idle(self._apply_wrap)

    def show_streamed_translation(self, translation: str) -> None:
        text = normalize_text(translation)
        if not text:
            return
        self.translation_var.set(text + " ▍")
        self.explain_button.configure(state="normal")
        self.after_idle(self._apply_wrap)

    def show(self) -> None:
        self.deiconify()
        # Do not lift or force focus. All application windows use the normal
        # Windows z-order and must yield to whichever software the user selects.

    def hide(self) -> None:
        self.withdraw()


class BilingualLogWindow(tk.Toplevel):
    """Scrollable bilingual reading history with sentence-level explanation."""

    def __init__(self, owner: "Application"):
        super().__init__(owner)
        self.owner = owner
        self.current_quest = ""
        self.selected_row_id: int | None = None
        self.row_ranges: dict[int, tuple[str, str]] = {}
        self.title("FGO 双语 LOG")
        self.configure(bg=UI_BG)
        self.attributes("-topmost", False)
        self.geometry("940x700")
        self.minsize(620, 420)
        self.protocol("WM_DELETE_WINDOW", self.hide)

        header_card = tk.Frame(
            self, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        header_card.pack(fill="x", padx=16, pady=(16, 10))
        header = tk.Frame(header_card, bg=UI_CARD)
        header.pack(fill="x", padx=18, pady=(14, 5))
        self.title_var = tk.StringVar(value="剧情 LOG")
        tk.Label(
            header,
            textvariable=self.title_var,
            bg=UI_CARD,
            fg=UI_TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self.back_button = apple_button(
            header, "返回当前译文", self.hide, compact=True
        )
        self.back_button.pack(side="right", padx=(8, 0))
        self.explain_button = apple_button(
            header, "解释这句", self.explain_selected, primary=True, compact=True
        )
        self.explain_button.configure(state="disabled")
        self.explain_button.pack(side="right")
        self.hint_var = tk.StringVar(value="点选任意一句，再查看结合整段剧情的解释")
        tk.Label(
            header_card,
            textvariable=self.hint_var,
            bg=UI_CARD,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(fill="x", padx=18, pady=(0, 13))

        body = tk.Frame(
            self, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.text = tk.Text(
            body,
            wrap="char",
            bg=UI_CARD,
            fg=UI_TEXT,
            insertbackground=UI_TEXT,
            selectbackground="#B9D8FF",
            selectforeground=UI_TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=24,
            pady=18,
            spacing1=2,
            spacing3=4,
            cursor="arrow",
            font=("Yu Gothic UI", 12),
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y", padx=(0, 3), pady=4)
        self.text.tag_configure(
            "speaker", foreground=UI_BLUE,
            font=("Microsoft YaHei UI", 10, "bold"), spacing1=14,
        )
        self.text.tag_configure(
            "source", foreground=UI_JAPANESE, font=("Yu Gothic UI", 12)
        )
        self.text.tag_configure(
            "translation", foreground=UI_TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
            lmargin1=12, lmargin2=12, spacing1=5,
        )
        self.text.tag_configure(
            "pending", foreground=UI_TERTIARY, font=("Microsoft YaHei UI", 10)
        )
        self.text.tag_configure("separator", foreground=UI_DIVIDER, spacing3=7)
        self.text.tag_configure("selected_row", background=UI_BLUE_TINT)
        self.text.bind("<Control-c>", self._copy_selection)
        self.text.bind("<Control-C>", self._copy_selection)
        self.text.bind("<MouseWheel>", self._mousewheel)
        self.text.bind("<Button-1>", self._select_at, add="+")
        self.text.bind("<Double-Button-1>", self._explain_at, add="+")
        self.withdraw()

    def _copy_selection(self, _event: tk.Event) -> str:
        try:
            selected = self.text.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(selected)
        self.update_idletasks()
        return "break"

    def _mousewheel(self, event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            steps = -1 if delta > 0 else 1
            self.text.yview_scroll(steps * 3, "units")
        return "break"

    def _row_at(self, x: int, y: int) -> int | None:
        index = self.text.index(f"@{x},{y}")
        for tag in self.text.tag_names(index):
            if tag.startswith("row_"):
                try:
                    return int(tag[4:])
                except ValueError:
                    return None
        return None

    def _select_at(self, event: tk.Event) -> None:
        row_id = self._row_at(int(event.x), int(event.y))
        if row_id is not None:
            self.select_row(row_id)

    def _explain_at(self, event: tk.Event) -> str | None:
        row_id = self._row_at(int(event.x), int(event.y))
        if row_id is None:
            return None
        self.select_row(row_id)
        self.explain_selected()
        return "break"

    def select_row(self, row_id: int) -> None:
        target = self.row_ranges.get(int(row_id))
        if not target:
            return
        self.selected_row_id = int(row_id)
        self.text.tag_remove("selected_row", "1.0", "end")
        self.text.tag_add("selected_row", target[0], target[1])
        self.text.tag_lower("selected_row")
        self.explain_button.configure(state="normal")
        self.hint_var.set("已选中一句 · 双击也可以直接解释")

    def explain_selected(self) -> None:
        if self.selected_row_id is None:
            self.bell()
            return
        self.owner.explain_row(self.selected_row_id)

    def is_visible(self) -> bool:
        return self.state() != "withdrawn"

    def open(self, quest: str) -> None:
        self.current_quest = normalize_text(quest) or "未知关卡"
        self.deiconify()
        try:
            self.state("normal")
        except tk.TclError:
            pass
        self.update_idletasks()
        self.refresh(scroll_to_bottom=True)
        # This is a direct response to the user's LOG-button click. Raising a
        # normal window once is not persistent topmost behavior.
        self.lift()
        self.focus_set()

    def hide(self) -> None:
        self.withdraw()
        if self.owner.config_data.show_translation_overlay:
            self.owner.overlay.show()

    def refresh(self, quest: str | None = None, scroll_to_bottom: bool = False) -> None:
        if quest:
            self.current_quest = normalize_text(quest) or "未知关卡"
        if not self.current_quest:
            return
        rows = [
            row for row in self.owner.store.rows(self.current_quest)
            if str(row[8]) != "skipped"
        ]
        was_at_bottom = self.text.yview()[1] >= 0.985
        previous_top = self.text.yview()[0]
        previous_selection = self.selected_row_id
        self.row_ranges.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for row in rows:
            (
                row_id, _captured_at, _quest, speaker, original_text, _source,
                translation, _model, status, _updated,
            ) = row
            row_id = int(row_id)
            start = self.text.index("end-1c")
            display_speaker = self.owner.display_speaker(
                self.current_quest, str(speaker)
            )
            if display_speaker:
                nameplate = (
                    display_speaker
                    if display_speaker.startswith("【") and display_speaker.endswith("】")
                    else f"【{display_speaker}】"
                )
                self.text.insert("end", f"{nameplate}\n", ("speaker", f"row_{row_id}"))
            self.text.insert(
                "end", f"{normalize_text(str(original_text))}\n",
                ("source", f"row_{row_id}"),
            )
            translated = normalize_text(str(translation))
            if translated:
                self.text.insert(
                    "end", f"{translated}\n", ("translation", f"row_{row_id}")
                )
            elif str(status) == "error":
                self.text.insert(
                    "end", "翻译失败，可在主窗口重试\n", ("pending", f"row_{row_id}")
                )
            else:
                self.text.insert(
                    "end", "正在翻译…\n", ("pending", f"row_{row_id}")
                )
            self.text.insert(
                "end", "────────────────────────\n",
                ("separator", f"row_{row_id}"),
            )
            end = self.text.index("end-1c")
            self.row_ranges[row_id] = (start, end)
        if not rows:
            self.text.insert("end", "还没有可显示的剧情记录。", "pending")
        self.text.configure(state="disabled")
        self.title_var.set(f"{self.current_quest}  ·  {len(rows)} 条")
        available_ids = set(self.row_ranges)
        if previous_selection in available_ids:
            self.select_row(int(previous_selection))
        elif self.owner.current_row_id in available_ids:
            self.select_row(int(self.owner.current_row_id))
        elif rows:
            self.select_row(int(rows[-1][0]))
        else:
            self.selected_row_id = None
            self.explain_button.configure(state="disabled")
            self.hint_var.set("点选任意一句，再查看结合整段剧情的解释")
        if scroll_to_bottom or was_at_bottom:
            self.text.see("end")
        else:
            self.text.yview_moveto(previous_top)


class ExplanationWindow(tk.Toplevel):
    """A quiet reading sheet for contextual Codex explanations."""

    def __init__(self, owner: "Application"):
        super().__init__(owner)
        self.owner = owner
        self.row_id: int | None = None
        self.title("FGO 剧情解释")
        self.configure(bg=UI_BG)
        self.attributes("-topmost", False)
        self.geometry("780x680")
        self.minsize(600, 460)
        self.protocol("WM_DELETE_WINDOW", self.hide)

        header = tk.Frame(
            self, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        header.pack(fill="x", padx=16, pady=(16, 10))
        title_row = tk.Frame(header, bg=UI_CARD)
        title_row.pack(fill="x", padx=20, pady=(16, 7))
        tk.Label(
            title_row, text="理解这句话", bg=UI_CARD, fg=UI_TEXT,
            font=("Microsoft YaHei UI", 15, "bold"), anchor="w",
        ).pack(side="left")
        self.close_button = apple_button(title_row, "完成", self.hide, compact=True)
        self.close_button.pack(side="right")
        self.quest_var = tk.StringVar(value="")
        self.target_var = tk.StringVar(value="")
        tk.Label(
            header, textvariable=self.quest_var, bg=UI_CARD, fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9), anchor="w",
        ).pack(fill="x", padx=20)
        self.target_label = tk.Label(
            header, textvariable=self.target_var, bg=UI_CARD, fg=UI_JAPANESE,
            font=("Yu Gothic UI", 11), justify="left", anchor="nw",
            wraplength=700,
        )
        self.target_label.pack(fill="x", padx=20, pady=(7, 16))

        body = tk.Frame(
            self, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        body.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.text = tk.Text(
            body, wrap="char", bg=UI_CARD, fg=UI_TEXT,
            selectbackground="#B9D8FF", selectforeground=UI_TEXT,
            relief="flat", borderwidth=0, highlightthickness=0,
            padx=24, pady=20, font=("Microsoft YaHei UI", 11),
            spacing3=4,
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y", padx=(0, 3), pady=4)
        self.text.tag_configure(
            "heading", foreground=UI_BLUE,
            font=("Microsoft YaHei UI", 11, "bold"), spacing1=12, spacing3=6,
        )
        self.text.tag_configure(
            "body", foreground=UI_TEXT,
            font=("Microsoft YaHei UI", 11), spacing3=8,
        )
        self.text.tag_configure(
            "loading", foreground=UI_SECONDARY,
            font=("Microsoft YaHei UI", 11), justify="center", spacing1=80,
        )
        self.text.tag_configure("link", foreground=UI_BLUE, underline=True)
        self.text.tag_bind("link", "<Enter>", lambda _event: self.text.configure(cursor="hand2"))
        self.text.tag_bind("link", "<Leave>", lambda _event: self.text.configure(cursor="xterm"))
        self.text.tag_bind("link", "<Button-1>", self._open_link)
        self.text.configure(state="disabled")

        footer = tk.Frame(self, bg=UI_BG)
        footer.pack(fill="x", padx=16, pady=(0, 16))
        self.status_var = tk.StringVar(value="解释会自动缓存，重复查看不会消耗新的请求")
        tk.Label(
            footer, textvariable=self.status_var, bg=UI_BG, fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9), anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self.copy_button = apple_button(footer, "复制解释", self.copy, compact=True)
        self.copy_button.configure(state="disabled")
        self.copy_button.pack(side="right", padx=(8, 0))
        self.regenerate_button = apple_button(
            footer, "重新解释", self.regenerate, compact=True
        )
        self.regenerate_button.configure(state="disabled")
        self.regenerate_button.pack(side="right")
        self.bind("<Configure>", self._resize_wrap)
        self.withdraw()

    def _resize_wrap(self, event: tk.Event) -> None:
        if getattr(event, "widget", self) is self:
            self.target_label.configure(wraplength=max(360, self.winfo_width() - 82))

    def _set_text(self, value: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        blocks = [block.strip() for block in str(value).split("\n\n") if block.strip()]
        for block in blocks:
            title, separator, rest = block.partition("\n")
            self.text.insert("end", title + "\n", "heading")
            if separator and rest:
                cursor = 0
                for match in re.finditer(r"https?://[^\s]+", rest):
                    if match.start() > cursor:
                        self.text.insert("end", rest[cursor:match.start()], "body")
                    url = match.group(0).rstrip("。），)]}")
                    suffix = match.group(0)[len(url):]
                    self.text.insert("end", url, ("body", "link"))
                    if suffix:
                        self.text.insert("end", suffix, "body")
                    cursor = match.end()
                self.text.insert("end", rest[cursor:] + "\n", "body")
        if not blocks:
            self.text.insert("end", "暂时没有解释。", "loading")
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _open_link(self, event: tk.Event) -> str:
        index = self.text.index(f"@{event.x},{event.y}")
        ranges = self.text.tag_ranges("link")
        for start, end in zip(ranges[0::2], ranges[1::2]):
            if self.text.compare(start, "<=", index) and self.text.compare(index, "<", end):
                url = self.text.get(start, end).strip()
                if re.match(r"^https?://", url, flags=re.IGNORECASE):
                    webbrowser.open(url)
                break
        return "break"

    def _show_window(self) -> None:
        self.deiconify()
        try:
            self.state("normal")
        except tk.TclError:
            pass
        self.lift()
        self.focus_set()

    def show_loading(
        self, row_id: int, quest: str, speaker: str, ja: str, zh: str, line_count: int
    ) -> None:
        self.row_id = int(row_id)
        mode = "  ·  按需联网查找旧剧情" if self.owner.config_data.explanation_web_search else ""
        self.quest_var.set(f"{quest}  ·  正在阅读本关卡 {line_count} 条剧情{mode}")
        name = self.owner.display_speaker(quest, speaker)
        prefix = f"{name}\n" if name else ""
        translated = f"\n{zh}" if normalize_text(zh) else ""
        self.target_var.set(f"{prefix}{normalize_text(ja)}{translated}")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        loading = "Codex 正在结合整段剧情"
        if self.owner.config_data.explanation_web_search:
            loading += "，并按需查找以前发生的事情"
        self.text.insert("end", loading + "…", "loading")
        self.text.configure(state="disabled")
        self.status_var.set(
            "正在分析当前语境、旧剧情指代、语气与言外之意"
            if self.owner.config_data.explanation_web_search
            else "正在分析前因后果、语气与言外之意"
        )
        self.copy_button.configure(state="disabled")
        self.regenerate_button.configure(state="disabled")
        self._show_window()

    def show_result(
        self,
        row_id: int,
        quest: str,
        explanation: str,
        model: str,
        cached: bool = False,
        web_enabled: bool = False,
        source_count: int = 0,
    ) -> None:
        self.row_id = int(row_id)
        if quest:
            suffix = "已缓存" if cached else f"{model}"
            self.quest_var.set(f"{quest}  ·  {suffix}")
        self._set_text(explanation)
        if cached:
            self.status_var.set(
                "这是之前保存的联网解释，不会重复消耗请求"
                if web_enabled else "这是之前保存的解释，不会重复消耗请求"
            )
        elif web_enabled:
            suffix = f"，引用 {source_count} 个页面" if source_count else ""
            self.status_var.set(f"已结合当前关卡与旧剧情资料生成并保存{suffix}")
        else:
            self.status_var.set("已结合本关卡完整剧情生成并保存")
        self.copy_button.configure(state="normal")
        self.regenerate_button.configure(state="normal")
        self._show_window()

    def show_error(self, message: str) -> None:
        self._set_text(f"暂时无法解释\n{message}")
        self.status_var.set("你可以稍后点击“重新解释”")
        self.regenerate_button.configure(state="normal")
        self._show_window()

    def copy(self) -> None:
        value = self.text.get("1.0", "end-1c").strip()
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.status_var.set("解释已复制")

    def regenerate(self) -> None:
        if self.row_id is not None:
            self.owner.explain_row(self.row_id, force=True)

    def hide(self) -> None:
        self.withdraw()


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1480x760")
        self.minsize(1000, 560)
        self.configure(bg=UI_BG)
        # Every FGO listener window deliberately uses normal Windows z-order.
        self.attributes("-topmost", False)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.store = HistoryStore(data_dir() / "history.db")
        self.config_data = RuntimeConfig.load()
        self.knowledge = KnowledgeBase(
            data_dir() / "knowledge", bundled_knowledge_path()
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker: ListenerThread | None = None
        self.translator: CodexTranslationWorker | None = None
        self.preload_translator: CodexTranslationWorker | None = None
        self.explanation_worker: CodexExplanationWorker | None = None
        self.knowledge_update_thread: threading.Thread | None = None
        self.knowledge_cancel = threading.Event()
        self.status_var = tk.StringVar(value="尚未连接")
        self.translation_status_var = tk.StringVar(value="AI 翻译尚未启动")
        self.knowledge_status_var = tk.StringVar(value=self._knowledge_status_text())
        self.count_var = tk.StringVar(value="0 条")
        self.auto_scroll = tk.BooleanVar(value=True)
        # The main table is the database inspector, so show staged rows by
        # default. The game-style LOG continues to show played rows only.
        self.show_preloaded_rows = tk.BooleanVar(value=True)
        self.quest_filter = tk.StringVar(value="全部关卡")
        self.current_row_id: int | None = None
        self._active_preload_quest = ""
        self._session_anchor = self.store.max_order() + 1_000_000
        self._next_live_order = self._session_anchor
        self._build_ui()
        self.overlay = BilingualOverlay(self)
        self.log_window = BilingualLogWindow(self)
        self.explanation_window = ExplanationWindow(self)
        if not self.config_data.show_translation_overlay:
            self.overlay.hide()
        self._load_history()
        self.after(8, self._poll_events)
        self.after(180, self._start_translator)
        self.after(350, self.start_listener)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "App.Treeview",
            background=UI_CARD,
            fieldbackground=UI_CARD,
            foreground=UI_TEXT,
            borderwidth=0,
            relief="flat",
            rowheight=38,
            font=("Microsoft YaHei UI", 10),
        )
        style.map(
            "App.Treeview",
            background=[("selected", UI_BLUE_TINT)],
            foreground=[("selected", UI_TEXT)],
        )
        style.configure(
            "App.Treeview.Heading",
            background="#F8F8FA",
            foreground=UI_SECONDARY,
            bordercolor=UI_DIVIDER,
            lightcolor="#F8F8FA",
            darkcolor="#F8F8FA",
            relief="flat",
            padding=(10, 9),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map("App.Treeview.Heading", background=[("active", "#F0F0F3")])
        style.configure(
            "App.TCombobox",
            fieldbackground=UI_CARD,
            background=UI_CARD,
            foreground=UI_TEXT,
            bordercolor=UI_BORDER,
            arrowcolor=UI_SECONDARY,
            padding=(9, 5),
        )
        style.configure(
            "App.TCheckbutton",
            background=UI_CARD,
            foreground=UI_TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        style.map("App.TCheckbutton", background=[("active", UI_CARD)])

        page = tk.Frame(self, bg=UI_BG)
        page.pack(fill="both", expand=True, padx=20, pady=18)

        hero = tk.Frame(
            page, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        hero.pack(fill="x", pady=(0, 12))
        hero_left = tk.Frame(hero, bg=UI_CARD)
        hero_left.pack(side="left", fill="both", expand=True, padx=20, pady=16)
        tk.Label(
            hero_left,
            text="FGO 剧情助手",
            bg=UI_CARD,
            fg=UI_TEXT,
            font=("Microsoft YaHei UI", 17, "bold"),
            anchor="w",
        ).pack(fill="x")
        status_line = tk.Frame(hero_left, bg=UI_CARD)
        status_line.pack(fill="x", pady=(5, 0))
        self.status_dot = tk.Label(
            status_line, text="●", bg=UI_CARD, fg=UI_RED,
            font=("Segoe UI", 10),
        )
        self.status_dot.pack(side="left")
        tk.Label(
            status_line,
            textvariable=self.status_var,
            bg=UI_CARD,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", padx=(6, 0))

        hero_actions = tk.Frame(hero, bg=UI_CARD)
        hero_actions.pack(side="right", padx=20, pady=16)
        apple_button(
            hero_actions, "显示当前译文", self.show_overlay, primary=True
        ).pack(side="right")
        apple_button(
            hero_actions, "重新连接", self.restart_listener
        ).pack(side="right", padx=8)
        apple_button(hero_actions, "设置", self.open_settings).pack(side="right")

        controls = tk.Frame(
            page, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        controls.pack(fill="x", pady=(0, 12))
        controls_top = tk.Frame(controls, bg=UI_CARD)
        controls_top.pack(fill="x", padx=20, pady=(15, 9))
        tk.Label(
            controls_top,
            text="剧情记录",
            bg=UI_CARD,
            fg=UI_TEXT,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left")
        tk.Label(
            controls_top,
            textvariable=self.count_var,
            bg=UI_CARD,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            controls_top,
            text="显示后台预载",
            variable=self.show_preloaded_rows,
            command=self._preload_view_changed,
            style="App.TCheckbutton",
        ).pack(side="right")
        ttk.Checkbutton(
            controls_top,
            text="跟随最新内容",
            variable=self.auto_scroll,
            style="App.TCheckbutton",
        ).pack(side="right", padx=(0, 16))
        self.quest_combo = ttk.Combobox(
            controls_top,
            textvariable=self.quest_filter,
            state="readonly",
            width=32,
            values=("全部关卡",),
            style="App.TCombobox",
        )
        self.quest_combo.pack(side="right", padx=(8, 18))
        self.quest_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._apply_filter()
        )
        tk.Label(
            controls_top,
            text="关卡",
            bg=UI_CARD,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right")

        tk.Frame(controls, bg=UI_DIVIDER, height=1).pack(fill="x", padx=20)
        service_row = tk.Frame(controls, bg=UI_CARD)
        service_row.pack(fill="x", padx=20, pady=(10, 7))
        tk.Label(
            service_row,
            textvariable=self.translation_status_var,
            bg=UI_CARD,
            fg=UI_BLUE,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        apple_button(
            service_row, "重试未翻译", self.retry_translations, compact=True
        ).pack(side="right")
        apple_button(
            service_row, "仅保留译文窗", self.overlay_only, compact=True
        ).pack(side="right", padx=8)

        knowledge_row = tk.Frame(controls, bg=UI_CARD)
        knowledge_row.pack(fill="x", padx=20, pady=(0, 13))
        tk.Label(
            knowledge_row,
            textvariable=self.knowledge_status_var,
            bg=UI_CARD,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        self.rollback_knowledge_button = apple_button(
            knowledge_row, "回滚术语库", self.rollback_knowledge, compact=True
        )
        self.rollback_knowledge_button.pack(side="right")
        self.update_knowledge_button = apple_button(
            knowledge_row, "更新术语与口癖", self.update_knowledge, compact=True
        )
        self.update_knowledge_button.pack(side="right", padx=8)
        if not self.knowledge.summary()["can_rollback"]:
            self.rollback_knowledge_button.configure(state="disabled")

        table_card = tk.Frame(
            page, bg=UI_CARD, highlightbackground=UI_DIVIDER,
            highlightthickness=1, borderwidth=0,
        )
        table_card.pack(fill="both", expand=True, pady=(0, 12))
        columns = ("quest", "speaker", "text", "translation", "record_state")
        self.tree = ttk.Treeview(
            table_card,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="App.Treeview",
        )
        self.tree.heading("quest", text="关卡")
        self.tree.heading("speaker", text="人物")
        self.tree.heading("text", text="日文原文")
        self.tree.heading("translation", text="中文译文")
        self.tree.heading("record_state", text="状态")
        self.tree.column("quest", width=230, minwidth=160, stretch=False)
        self.tree.column("speaker", width=170, minwidth=110, stretch=False)
        self.tree.column("text", width=460, minwidth=280, stretch=True)
        self.tree.column("translation", width=450, minwidth=280, stretch=True)
        self.tree.column(
            "record_state", width=92, minwidth=82, stretch=False, anchor="center"
        )
        scroll_y = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        scroll_x = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        scroll_y.grid(row=0, column=1, sticky="ns", pady=3)
        scroll_x.grid(row=1, column=0, sticky="ew", padx=3)
        table_card.rowconfigure(0, weight=1)
        table_card.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _event: self.explain_selected_from_table())
        self.tree.bind("<Control-c>", self._copy_shortcut)
        self.tree.bind("<Control-C>", self._copy_shortcut)
        self.tree.tag_configure(
            "preloaded", background="#F2F7FC", foreground=UI_SECONDARY
        )

        footer = tk.Frame(page, bg=UI_BG)
        footer.pack(fill="x")
        apple_button(
            footer, "解释选中句", self.explain_selected_from_table, primary=True
        ).pack(side="left")
        apple_button(footer, "复制选中", self.copy_selected).pack(
            side="left", padx=(8, 0)
        )
        apple_button(footer, "复制全部", self.copy_all).pack(side="left", padx=8)
        apple_button(footer, "导出 TXT", self.export_text).pack(side="left")
        apple_button(
            footer, "清空历史", self.clear_history, destructive=True
        ).pack(side="right")
        tk.Label(
            footer,
            text="双击一行查看解释  ·  Ctrl+C 复制选中内容",
            bg=UI_BG,
            fg=UI_SECONDARY,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", padx=16)

    def _knowledge_status_text(self) -> str:
        summary = self.knowledge.summary()
        generated = str(summary.get("generated_at", ""))
        if generated:
            try:
                generated = datetime.fromisoformat(generated).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:
                generated = generated.replace("T", " ")
        location = "已更新" if summary.get("using_runtime") else "内置基线"
        source_text = f" · {summary.get('source_count', 0)} 个来源" if summary.get("source_count") else ""
        date_text = f" · {generated}" if generated else ""
        return (
            f"知识库：术语 {summary.get('term_count', 0)} 条 · "
            f"人物口癖 {summary.get('style_count', 0)} 条 · {location}"
            f"{source_text}{date_text}"
        )

    def _restart_translator_after_knowledge_change(self) -> None:
        if self.translator:
            self.translator.close()
            self.translator = None
        if self.preload_translator:
            self.preload_translator.close()
            self.preload_translator = None
        self._start_translator()

    def update_knowledge(self) -> None:
        if self.knowledge_update_thread and self.knowledge_update_thread.is_alive():
            self.bell()
            return
        self.knowledge_cancel = threading.Event()
        self.update_knowledge_button.configure(state="disabled")
        self.rollback_knowledge_button.configure(state="disabled")
        self.knowledge_status_var.set("知识库：正在安全更新术语与人物口癖；当前翻译仍可继续使用…")

        def progress(stage: str, current: int, total: int, message: str) -> None:
            self.events.put(
                {
                    "type": "knowledge_progress",
                    "stage": stage,
                    "current": current,
                    "total": total,
                    "text": message,
                }
            )

        def run_update() -> None:
            try:
                result = KnowledgeUpdater(
                    self.knowledge,
                    APP_VERSION,
                    progress,
                    self.knowledge_cancel,
                ).update()
                self.events.put({"type": "knowledge_complete", "result": result})
            except Exception as exc:
                self.events.put(
                    {
                        "type": "knowledge_error",
                        "message": str(exc),
                        "details": traceback.format_exc(),
                    }
                )

        self.knowledge_update_thread = threading.Thread(
            target=run_update, name="FgoKnowledgeUpdater", daemon=True
        )
        self.knowledge_update_thread.start()

    def rollback_knowledge(self) -> None:
        if self.knowledge_update_thread and self.knowledge_update_thread.is_alive():
            self.bell()
            return
        if not messagebox.askyesno(
            "回滚术语库",
            "切换回上一版术语库吗？当前版本会保留，可再次回滚切换回来。",
            parent=self,
        ):
            return
        try:
            self.knowledge.rollback()
        except KnowledgeUpdateError as exc:
            messagebox.showerror("回滚术语库", str(exc), parent=self)
            return
        self.knowledge_status_var.set(self._knowledge_status_text())
        self.rollback_knowledge_button.configure(
            state="normal" if self.knowledge.summary()["can_rollback"] else "disabled"
        )
        self._apply_filter()
        if self.current_row_id is not None:
            self._show_current_row(self.current_row_id)
        if self.log_window.is_visible():
            self.log_window.refresh()
        self._restart_translator_after_knowledge_change()
        self.status_var.set("已切换到上一版术语库；后续译文立即使用该版本")

    def _load_history(self) -> None:
        self._refresh_filter_values()
        for row in self.store.view_rows(
            include_preloaded=self.show_preloaded_rows.get()
        ):
            (
                row_id, _captured_at, quest, speaker, text, _source,
                translation, _model, status, _updated, preloaded,
            ) = row
            self._insert_tree(
                row_id, quest, speaker, text, translation, status, bool(preloaded)
            )
            if not preloaded and status != "skipped":
                self.current_row_id = int(row_id)
        self._update_count()
        if self.current_row_id is not None:
            self._show_current_row(self.current_row_id)

    @staticmethod
    def _translation_display(translation: str, status: str) -> str:
        if translation:
            return translation.replace("\n", " ⏎ ")
        if status == "skipped":
            return "已跳过（战斗/系统界面，不耗 AI）"
        if status == "error":
            return "⚠ 翻译失败，可点击“重试未翻译”"
        if status == "translating":
            return "正在翻译…"
        return "等待翻译…"

    def display_speaker(self, quest: str, speaker: str) -> str:
        """Show a verified/model-cached Chinese name while retaining Japanese."""
        original = sanitize_speaker(speaker)
        if not original:
            return ""
        translated = self.knowledge.translate_speaker(original)
        if not translated:
            translated = self.store.speaker_translation(quest, original)
        translated = normalize_text(translated)
        if not translated or translated == original:
            return original
        return f"{translated}（{original}）"

    def _insert_tree(
        self,
        row_id: int,
        quest: str,
        speaker: str,
        text: str,
        translation: str = "",
        status: str = "",
        preloaded: bool = False,
    ) -> None:
        one_line = text.replace("\n", " ⏎ ")
        values = (
            quest,
            self.display_speaker(quest, speaker),
            one_line,
            self._translation_display(translation, status),
            "后台预载" if preloaded else "已播放",
        )
        iid = str(row_id)
        if self.tree.exists(iid):
            self.tree.item(iid, values=values, tags=(("preloaded",) if preloaded else ()))
            item = iid
        else:
            item = self.tree.insert(
                "", "end", iid=iid, values=values,
                tags=(("preloaded",) if preloaded else ()),
            )
        if self.auto_scroll.get() and not preloaded:
            self.tree.see(item)

    def _refresh_filter_values(self) -> None:
        values = [
            "全部关卡",
            *self.store.quests(include_preloaded=self.show_preloaded_rows.get()),
        ]
        self.quest_combo.configure(values=values)
        if self.quest_filter.get() not in values:
            self.quest_filter.set("全部关卡")

    def _apply_filter(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        selected = self.quest_filter.get()
        quest = None if selected == "全部关卡" else selected
        for row in self.store.view_rows(
            quest, include_preloaded=self.show_preloaded_rows.get()
        ):
            (
                row_id, _captured_at, row_quest, speaker, text, _source,
                translation, _model, status, _updated, preloaded,
            ) = row
            self._insert_tree(
                row_id, row_quest, speaker, text, translation, status, bool(preloaded)
            )
        self._update_count()

    def _update_tree_rows(self, row_ids: list[int]) -> None:
        """Incrementally paint changed rows; never rebuild the full history hot path."""
        clean_ids = sorted({int(value) for value in row_ids if int(value) > 0})
        if not clean_ids:
            self._update_count()
            return
        selected = self.quest_filter.get()
        for item in self.store.translation_items(clean_ids):
            preloaded = bool(item.get("preloaded"))
            quest = str(item.get("quest", ""))
            if preloaded and not self.show_preloaded_rows.get():
                continue
            if selected != "全部关卡" and selected != quest:
                continue
            self._insert_tree(
                int(item["id"]),
                quest,
                str(item.get("speaker", "")),
                str(item.get("text", "")),
                str(item.get("translation", "")),
                str(item.get("status", "")),
                preloaded,
            )
        self._update_count()

    def _preload_view_changed(self) -> None:
        self._refresh_filter_values()
        self._apply_filter()

    def _copy_shortcut(self, _event: tk.Event) -> str:
        self.copy_selected()
        return "break"

    def _update_count(self) -> None:
        selected = self.quest_filter.get()
        quest = None if selected == "全部关卡" else selected
        total, preloaded = self.store.view_counts(
            quest, include_preloaded=self.show_preloaded_rows.get()
        )
        visible = total - preloaded
        self.count_var.set(
            f"{total} 条（已播放 {visible} / 后台预载 {preloaded}）"
        )

    def _start_translator(self) -> None:
        if not self.config_data.translation_enabled:
            self.translation_status_var.set("AI 翻译已关闭；原文监听不受影响")
            return
        live_running = bool(self.translator and self.translator.is_alive())
        preload_running = bool(
            self.preload_translator and self.preload_translator.is_alive()
        )
        if live_running and preload_running:
            return
        if not live_running:
            self.translator = CodexTranslationWorker(
                self.store, self.config_data, self.events, scope="live"
            )
            self.translator.start()
        if not preload_running:
            self.preload_translator = CodexTranslationWorker(
                self.store, self.config_data, self.events, scope="preload"
            )
            self.preload_translator.start()
        pending = self.store.reset_translation_errors()
        pending_items = self.store.translation_items(pending)
        live_pending = [int(item["id"]) for item in pending_items if not item["preloaded"]]
        reset_preload_pending = [
            int(item["id"]) for item in pending_items if item["preloaded"]
        ]
        if self.translator:
            self.translator.enqueue(live_pending)
        preload_pending: list[int] = []
        if self.config_data.pretranslate_scenario:
            if self._active_preload_quest:
                preload_pending = self.store.preloaded_untranslated_ids(
                    self._active_preload_quest
                )
            else:
                # On application restart the game may already be inside a
                # parsed stage, so AnalysScript will not necessarily fire
                # again. Resume the newest staged scenario from the database.
                preload_pending = self.store.latest_preloaded_untranslated_ids()
            already_pending = set(reset_preload_pending)
            preload_pending = [
                row_id for row_id in preload_pending if row_id not in already_pending
            ]
            if self.preload_translator:
                self.preload_translator.enqueue(
                    [*reset_preload_pending, *preload_pending]
                )
        if self.current_row_id is not None:
            self.translator.enqueue_current(self.current_row_id)
        self.translation_status_var.set(
            f"实时：{self.config_data.translation_model}/{self.config_data.translation_reasoning}"
            + ("/Fast" if fast_mode_effective(self.config_data) else "")
            + ("/常驻" if self.config_data.translation_engine == "app-server" else "/兼容")
            + f" · 预翻译：{self.config_data.preload_translation_model}/"
            f"{self.config_data.preload_translation_reasoning}"
            + ("/Fast" if fast_mode_effective(self.config_data, preloaded=True) else "")
            + (
                f"；实时等待 {len(live_pending)} 条，剧情预翻译 "
                f"{len(reset_preload_pending) + len(preload_pending)} 条"
                if pending or preload_pending else ""
            )
        )

    def retry_translations(self) -> None:
        if not self.config_data.translation_enabled:
            self.bell()
            self.translation_status_var.set("请先在“设置”中启用 AI 翻译")
            return
        self._start_translator()
        ids = self.store.reset_translation_errors()
        items = self.store.translation_items(ids)
        live_ids = [int(item["id"]) for item in items if not item["preloaded"]]
        preload_ids = [int(item["id"]) for item in items if item["preloaded"]]
        if self.translator:
            self.translator.enqueue(live_ids)
            if self.current_row_id is not None:
                self.translator.enqueue_current(self.current_row_id)
        if self.preload_translator:
            self.preload_translator.enqueue(preload_ids)
        self._apply_filter()
        self.translation_status_var.set(
            f"已重新加入队列：实时 {len(live_ids)} 条 / 预翻译 {len(preload_ids)} 条"
        )

    def show_overlay(self) -> None:
        self.config_data.show_translation_overlay = True
        self.config_data.save()
        self.overlay.show()

    def overlay_only(self) -> None:
        self.show_overlay()
        self.withdraw()

    def show_log(self) -> None:
        quest = ""
        if self.current_row_id is not None:
            items = self.store.translation_items([self.current_row_id])
            if items:
                quest = str(items[0].get("quest", ""))
        if not quest:
            selected = self.quest_filter.get()
            quest = selected if selected != "全部关卡" else "未知关卡"
        self.overlay.hide()
        self.log_window.open(quest)

    def explain_current(self) -> None:
        if self.current_row_id is None:
            self.bell()
            return
        self.explain_row(self.current_row_id)

    def explain_selected_from_table(self) -> None:
        selected = self.tree.selection()
        if not selected:
            self.bell()
            self.status_var.set("请先选择一条要解释的剧情")
            return
        try:
            row_id = int(selected[-1])
        except (TypeError, ValueError):
            self.bell()
            return
        self.explain_row(row_id)

    def explain_row(self, row_id: int, force: bool = False) -> None:
        """Explain a selected line using every available row in its stage."""
        row_id = int(row_id)
        payload = self.store.explanation_context(row_id)
        if not payload:
            self.bell()
            self.status_var.set("找不到这条剧情，可能已经被清除")
            return
        target = payload["target"]
        cached = None if force else self.store.explanation(row_id)
        # Explanations saved by older builds only knew the current stage. When
        # web research is enabled, refresh such a cache once instead of showing
        # the same avoidable "无法确认" answer forever.
        if (
            cached
            and self.config_data.explanation_web_search
            and not bool(cached.get("web_enabled"))
        ):
            cached = None
        if cached:
            self.explanation_window.show_loading(
                row_id,
                str(payload["quest"]),
                str(target.get("speaker", "")),
                str(target.get("ja", "")),
                str(target.get("zh", "")),
                len(payload.get("scenario", [])),
            )
            self.explanation_window.show_result(
                row_id,
                str(payload["quest"]),
                str(cached["explanation"]),
                str(cached["model"]),
                cached=True,
                web_enabled=bool(cached.get("web_enabled")),
            )
            return
        if self.explanation_worker and self.explanation_worker.is_alive():
            if self.explanation_worker.row_id == row_id:
                self.explanation_window._show_window()
            else:
                self.bell()
                self.status_var.set("上一条剧情解释仍在生成，请稍候")
            return
        self.explanation_window.show_loading(
            row_id,
            str(payload["quest"]),
            str(target.get("speaker", "")),
            str(target.get("ja", "")),
            str(target.get("zh", "")),
            len(payload.get("scenario", [])),
        )
        self.explanation_worker = CodexExplanationWorker(
            self.store, self.config_data, row_id, self.events
        )
        self.explanation_worker.start()

    def _show_current_row(self, row_id: int) -> None:
        items = self.store.translation_items([row_id])
        if not items:
            return
        item = items[0]
        if str(item.get("status", "")) == "skipped":
            return
        self.current_row_id = row_id
        self.overlay.show_line(
            str(item["quest"]), str(item["speaker"]), str(item["text"]),
            str(item["translation"]),
        )
        if self.config_data.show_translation_overlay and not self.log_window.is_visible():
            self.overlay.show()

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("AI 翻译设置")
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(bg=UI_BG)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)

        enabled = tk.BooleanVar(value=self.config_data.translation_enabled)
        overlay = tk.BooleanVar(value=self.config_data.show_translation_overlay)
        pretranslate = tk.BooleanVar(value=self.config_data.pretranslate_scenario)
        explanation_web = tk.BooleanVar(
            value=self.config_data.explanation_web_search
        )
        live_model = tk.StringVar(value=self.config_data.translation_model)
        live_reasoning = tk.StringVar(value=self.config_data.translation_reasoning)
        live_fast = tk.BooleanVar(value=self.config_data.translation_fast_mode)
        preload_model = tk.StringVar(value=self.config_data.preload_translation_model)
        preload_reasoning = tk.StringVar(
            value=self.config_data.preload_translation_reasoning
        )
        preload_fast = tk.BooleanVar(
            value=self.config_data.preload_translation_fast_mode
        )
        engine = tk.StringVar(
            value=(
                "极速常驻（推荐）"
                if self.config_data.translation_engine == "app-server"
                else "兼容模式（每批重启）"
            )
        )
        batch_size = tk.StringVar(value=str(self.config_data.translation_batch_size))
        codex_path = tk.StringVar(value=self.config_data.codex_path)

        ttk.Checkbutton(frame, text="启用 codex-cli 自动翻译", variable=enabled).grid(
            row=0, column=0, sticky="w", pady=(0, 12)
        )
        ttk.Label(
            frame,
            text="剧情选项只会在游戏打开选择框时出现，因此使用实时模型。",
            foreground=UI_SECONDARY,
        ).grid(row=0, column=1, sticky="e", pady=(0, 12))

        live_frame = ttk.LabelFrame(
            frame, text="实时翻译 · 当前句 / 剧情选项 / 游戏 LOG", padding=12
        )
        live_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(live_frame, text="模型：").grid(
            row=0, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Combobox(
            live_frame,
            textvariable=live_model,
            values=available_codex_models(),
            width=34,
        ).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(live_frame, text="推理强度：").grid(
            row=1, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Combobox(
            live_frame,
            textvariable=live_reasoning,
            values=("low", "medium", "high", "xhigh"),
            state="readonly",
            width=31,
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(live_frame, text="运行模式：").grid(
            row=2, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Combobox(
            live_frame,
            textvariable=engine,
            values=("极速常驻（推荐）", "兼容模式（每批重启）"),
            state="readonly",
            width=31,
        ).grid(row=2, column=1, sticky="w", pady=5)
        ttk.Checkbutton(
            live_frame,
            text="Fast 模式（priority 服务层）",
            variable=live_fast,
        ).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Label(
            live_frame,
            text="推荐：gpt-5.6-terra / low / Fast。常驻会话优先响应刚出现的选项。",
            foreground=UI_SECONDARY,
        ).grid(row=4, column=1, sticky="w", pady=(2, 4))
        live_frame.columnconfigure(1, weight=1)

        preload_frame = ttk.LabelFrame(
            frame, text="完整剧情预翻译 · 整关自适应批量", padding=12
        )
        preload_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(preload_frame, text="模型：").grid(
            row=0, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Combobox(
            preload_frame,
            textvariable=preload_model,
            values=available_codex_models(),
            width=34,
        ).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(preload_frame, text="推理强度：").grid(
            row=1, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Combobox(
            preload_frame,
            textvariable=preload_reasoning,
            values=("low", "medium", "high", "xhigh", "max"),
            state="readonly",
            width=31,
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(
            preload_frame,
            text="推荐：gpt-5.6-sol / high。与实时常驻会话完全隔离。",
            foreground=UI_SECONDARY,
        ).grid(row=2, column=1, sticky="w", pady=(2, 4))
        ttk.Checkbutton(
            preload_frame,
            text="Fast 模式（后台仍使用高质量推理）",
            variable=preload_fast,
        ).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Checkbutton(
            preload_frame,
            text="进入剧情后读取完整脚本并在后台预翻译",
            variable=pretranslate,
        ).grid(row=4, column=1, sticky="w", pady=5)
        ttk.Checkbutton(
            preload_frame,
            text="解释这句时按需联网查找旧剧情与角色出处（推荐）",
            variable=explanation_web,
        ).grid(row=5, column=1, sticky="w", pady=5)
        ttk.Label(
            preload_frame,
            text="仅“解释这句”使用联网搜索；不会拖慢实时翻译或完整剧情预翻译。",
            foreground=UI_SECONDARY,
        ).grid(row=6, column=1, sticky="w", pady=(0, 4))
        preload_frame.columnconfigure(1, weight=1)

        common_frame = ttk.LabelFrame(frame, text="其他", padding=12)
        common_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Label(common_frame, text="普通补译每批：").grid(
            row=0, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Spinbox(
            common_frame, from_=1, to=100, textvariable=batch_size, width=8
        ).grid(row=0, column=1, sticky="w", pady=5)
        ttk.Checkbutton(
            common_frame,
            text="显示当前双语译文窗口（普通层级）",
            variable=overlay,
        ).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(common_frame, text="codex 路径：").grid(
            row=2, column=0, sticky="e", padx=(0, 8), pady=5
        )
        ttk.Entry(common_frame, textvariable=codex_path, width=42).grid(
            row=2, column=1, sticky="ew", pady=5
        )
        ttk.Label(
            common_frame,
            text="通常留空即可自动发现；不会修改你的 Codex 全局默认模型。",
            foreground=UI_SECONDARY,
        ).grid(row=3, column=1, sticky="w")
        common_frame.columnconfigure(1, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=2, sticky="e", pady=(16, 0))

        def save() -> None:
            try:
                size = max(1, min(100, int(batch_size.get())))
            except ValueError:
                messagebox.showerror("设置", "每批条数必须是 1 到 100 的整数。", parent=dialog)
                return
            selected_live_model = live_model.get().strip()
            selected_preload_model = preload_model.get().strip()
            if not selected_live_model or not selected_preload_model:
                messagebox.showerror("设置", "实时模型和预翻译模型都不能为空。", parent=dialog)
                return
            if live_fast.get() and selected_live_model not in codex_fast_models():
                messagebox.showerror(
                    "实时 Fast 模式",
                    f"{selected_live_model} 当前模型目录没有 Fast 服务层。",
                    parent=dialog,
                )
                return
            if preload_fast.get() and selected_preload_model not in codex_fast_models():
                messagebox.showerror(
                    "预翻译 Fast 模式",
                    f"{selected_preload_model} 当前模型目录没有 Fast 服务层。",
                    parent=dialog,
                )
                return
            if self.translator:
                self.translator.close()
                self.translator = None
            if self.preload_translator:
                self.preload_translator.close()
                self.preload_translator = None
            self.config_data.translation_enabled = enabled.get()
            self.config_data.translation_model = selected_live_model
            self.config_data.translation_reasoning = live_reasoning.get()
            self.config_data.translation_fast_mode = live_fast.get()
            self.config_data.preload_translation_model = selected_preload_model
            self.config_data.preload_translation_reasoning = preload_reasoning.get()
            self.config_data.preload_translation_fast_mode = preload_fast.get()
            self.config_data.translation_engine = (
                "app-server" if engine.get().startswith("极速常驻") else "exec"
            )
            self.config_data.translation_batch_size = size
            self.config_data.show_translation_overlay = overlay.get()
            self.config_data.pretranslate_scenario = pretranslate.get()
            self.config_data.explanation_web_search = explanation_web.get()
            self.config_data.codex_path = codex_path.get().strip()
            self.config_data.save()
            if overlay.get():
                self.overlay.show()
            else:
                self.overlay.hide()
            dialog.destroy()
            self._start_translator()

        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="保存并应用", command=save).pack(side="right", padx=(0, 8))
        dialog.grab_set()
        dialog.wait_visibility()
        dialog.focus_set()

    def start_listener(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.status_var.set("正在初始化…")
        self.status_dot.configure(fg=UI_ORANGE)
        self.worker = ListenerThread(self.config_data, self.events)
        self.worker.start()

    def restart_listener(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=2)
        self.worker = None
        self._session_anchor = self.store.max_order() + 1_000_000
        self._next_live_order = self._session_anchor
        self.start_listener()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(8, self._poll_events)

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "status":
            self.status_var.set(str(event.get("text", "")))
            self.status_dot.configure(fg="#2d9b55" if event.get("ready") else "#d18b00")
        elif kind == "ready":
            hooks = len(event.get("hooks", []))
            backlog_count = int(event.get("backlog_count", 0) or 0)
            suffix = f"，已读取游戏 LOG {backlog_count} 条" if backlog_count else ""
            self.status_var.set(f"IL2CPP Hook 已就绪（{hooks} 个{suffix}）")
            self.status_dot.configure(fg="#2d9b55")
        elif kind == "scenario_plan":
            if not (
                self.config_data.translation_enabled
                and self.config_data.pretranslate_scenario
            ):
                return
            timestamp_ms = int(event.get("captured_at_ms", 0) or 0)
            captured_at = (
                datetime.fromtimestamp(timestamp_ms / 1000)
                if timestamp_ms else datetime.now()
            )
            label = quest_label(event.get("quest"))
            entries = [
                value for value in event.get("entries", []) if isinstance(value, dict)
            ]
            pending_ids, total = self.store.add_preloaded(
                entries,
                label,
                captured_at,
                str(event.get("source", "ScriptManager.AnalysScript+Preload")),
            )
            self._active_preload_quest = label
            if pending_ids:
                self._start_translator()
                if self.preload_translator:
                    self.preload_translator.enqueue(pending_ids)
            self._refresh_filter_values()
            # Preloaded rows are hidden by default.  Even when explicitly
            # shown, update only this stage instead of rebuilding thousands of
            # existing Treeview items on the first visible line.
            if self.show_preloaded_rows.get():
                preload_ids = [
                    int(row[0]) for row in self.store.view_rows(
                        label, include_preloaded=True
                    ) if bool(row[10])
                ]
                self._update_tree_rows(preload_ids)
            else:
                self._update_count()
            cached = total - len(pending_ids)
            self.status_var.set(
                f"已读取完整剧情脚本 {total} 个片段：缓存命中 {cached}，"
                f"后台预翻译 {len(pending_ids)}（安全预算内整关单批，超限每批 100）"
            )
        elif kind == "backlog":
            timestamp_ms = int(event.get("captured_at_ms", 0) or 0)
            captured_at = datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.now()
            entries = [
                value for value in event.get("entries", []) if isinstance(value, dict)
            ]
            label = quest_label(event.get("quest"))
            ids = self.store.add_backlog(
                entries,
                label,
                captured_at,
                self._session_anchor,
                str(event.get("source", "ScriptBackLog.logData")),
            )
            self._refresh_filter_values()
            if ids and self.translator:
                self.translator.enqueue(ids)
            current_id = None
            if entries:
                last_entry = entries[-1]
                current_id = self.store.latest_visible_id(
                    label,
                    str(last_entry.get("speaker", "")),
                    str(last_entry.get("text", "")),
                )
            if current_id is not None:
                self._show_current_row(current_id)
                if self.translator:
                    self.translator.enqueue_current(current_id)
            self._update_tree_rows(ids + ([current_id] if current_id else []))
            if self.log_window.is_visible():
                self.log_window.refresh(label)
            self.status_var.set(f"已自动读取游戏 LOG：新增 {len(ids)} 条")
        elif kind == "choices":
            choices = [
                normalize_text(str(value)) for value in event.get("choices", [])
                if normalize_text(str(value))
            ]
            if choices:
                text = "\n".join(f"{index + 1}. {value}" for index, value in enumerate(choices))
                converted = dict(event)
                converted.update(
                    {
                        "type": "dialogue",
                        "speaker": "【剧情选项】",
                        "text": text,
                        "raw_speaker": "【剧情选项】",
                        "raw_text": "\n".join(
                            str(value) for value in event.get("raw_choices", event.get("choices", []))
                        ),
                    }
                )
                self._handle_event(converted)
        elif kind == "choice_selected":
            text = normalize_text(str(event.get("text", "")))
            index = int(event.get("index", -1) or 0)
            if text:
                converted = dict(event)
                converted.update(
                    {
                        "type": "dialogue",
                        "speaker": "【已选择】",
                        "text": f"{index + 1}. {text}" if index >= 0 else text,
                        "raw_speaker": "【已选择】",
                        "raw_text": str(event.get("raw_text", event.get("text", ""))),
                        "choice_index": index,
                        "choice_text": text,
                    }
                )
                self._handle_event(converted)
        elif kind == "dialogue_preview":
            speaker = sanitize_speaker(str(event.get("speaker", "")))
            text = normalize_text(str(event.get("text", "")))
            source = str(event.get("source", ""))
            if (
                text
                and not translation_skip_reason(speaker, text, source)
                and self.config_data.show_translation_overlay
                and not self.log_window.is_visible()
            ):
                self.overlay.show_preview(
                    quest_label(event.get("quest")), speaker, text
                )
                self.overlay.show()
        elif kind == "dialogue":
            timestamp_ms = int(event.get("captured_at_ms", 0) or 0)
            captured_at = datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.now()
            label = quest_label(event.get("quest"))
            speaker = str(event.get("speaker", ""))
            text = str(event.get("text", ""))
            source = str(event.get("source", ""))
            skip_translation = bool(translation_skip_reason(speaker, text, source))
            display_order = self._next_live_order
            self._next_live_order += 10
            raw_speaker = str(event.get("raw_speaker", event.get("speaker", "")))
            raw_text = str(event.get("raw_text", event.get("text", "")))
            row_id = self.store.activate_preloaded(
                speaker,
                text,
                source,
                captured_at,
                label,
                raw_speaker,
                raw_text,
                display_order,
            )
            if row_id is None:
                row_id = self.store.add(
                    speaker,
                    text,
                    source,
                    captured_at,
                    label,
                    raw_speaker,
                    raw_text,
                    display_order,
                )
            if row_id is not None:
                if speaker != "【剧情选项】" and speaker != "【已选择】":
                    self.store.link_preloaded_composition(row_id)
                linked_choice = False
                if speaker == "【已选择】":
                    linked_choice = self.store.link_selected_choice(
                        row_id,
                        label,
                        int(event.get("choice_index", -1)),
                        str(event.get("choice_text", text)),
                        display_order,
                    )
                stored = self.store.translation_items([row_id])
                stored_translation = str(stored[0]["translation"]) if stored else ""
                stored_status = str(stored[0]["status"]) if stored else ""
                stored_speaker = str(stored[0]["speaker"]) if stored else speaker
                # The overlay is the latency-critical surface. Update it before
                # touching the large database Treeview or rebuilding LOG text.
                if not skip_translation:
                    self._show_current_row(row_id)
                self._refresh_filter_values()
                selected = self.quest_filter.get()
                if selected == "全部关卡" or selected == label:
                    self._insert_tree(
                        row_id,
                        label,
                        sanitize_speaker(stored_speaker),
                        normalize_text(text),
                        translation=stored_translation,
                        status="skipped" if skip_translation else stored_status,
                    )
                    self._update_count()
                if (
                    self.translator
                    and not skip_translation
                    and not linked_choice
                ):
                    # A cache miss must never wait for the long Sol preload
                    # request. The currently visible page gets the dedicated
                    # latest-only Terra/Fast lane; completed preload hits are
                    # filtered locally and therefore consume no extra request.
                    self.translator.enqueue_current(row_id)
                if self.log_window.is_visible() and not skip_translation:
                    self.log_window.refresh(label)
        elif kind == "explanation_started":
            research = "并按需联网查找旧剧情" if event.get("web_search") else ""
            self.status_var.set(
                f"Codex 正在阅读本关卡 {int(event.get('line_count', 0) or 0)} 条剧情"
                f"{research}，正在解释选中句…"
            )
        elif kind == "explanation_complete":
            self.explanation_worker = None
            explained_row_id = int(event.get("row_id", 0) or 0)
            if self.explanation_window.row_id == explained_row_id:
                self.explanation_window.show_result(
                    explained_row_id,
                    str(event.get("quest", "")),
                    str(event.get("explanation", "")),
                    str(event.get("model", "")),
                    web_enabled=bool(event.get("web_search")),
                    source_count=int(event.get("source_count", 0) or 0),
                )
            self.status_var.set(
                "剧情解释已结合旧剧情资料生成并缓存"
                if event.get("web_search") else "剧情解释已生成并缓存"
            )
        elif kind == "explanation_error":
            self.explanation_worker = None
            if self.explanation_window.row_id == int(event.get("row_id", 0) or 0):
                self.explanation_window.show_error(str(event.get("message", "解释失败")))
            self.status_var.set(f"剧情解释失败：{event.get('message', '')}")
        elif kind == "knowledge_progress":
            current = int(event.get("current", 0) or 0)
            total = int(event.get("total", 0) or 0)
            progress = f" {current}/{total}" if total > 1 else ""
            self.knowledge_status_var.set(
                f"术语库更新{progress}：{event.get('text', '正在处理…')}"
            )
        elif kind == "knowledge_complete":
            result = event.get("result", {}) if isinstance(event.get("result"), dict) else {}
            self.knowledge.refresh()
            self._apply_filter()
            if self.current_row_id is not None:
                self._show_current_row(self.current_row_id)
            if self.log_window.is_visible():
                self.log_window.refresh()
            self.update_knowledge_button.configure(state="normal")
            self.rollback_knowledge_button.configure(
                state="normal" if self.knowledge.summary()["can_rollback"] else "disabled"
            )
            self.knowledge_status_var.set(self._knowledge_status_text())
            self._restart_translator_after_knowledge_change()
            self.status_var.set(
                f"知识学习完成：术语 {int(result.get('term_count', 0) or 0)} 条，"
                f"人物口癖 {int(result.get('style_count', 0) or 0)} 条；"
                f"冲突 {int(result.get('conflicts', 0) or 0)} 条已隔离，旧版可回滚"
            )
        elif kind == "knowledge_error":
            self.update_knowledge_button.configure(state="normal")
            self.rollback_knowledge_button.configure(
                state="normal" if self.knowledge.summary()["can_rollback"] else "disabled"
            )
            self.knowledge_status_var.set(self._knowledge_status_text())
            self.status_var.set(f"术语库更新失败，仍在使用旧版：{event.get('message', '')}")
        elif kind == "translation_status":
            self.translation_status_var.set(str(event.get("text", "正在翻译…")))
            for row_id in event.get("ids", []):
                if self.tree.exists(str(row_id)):
                    values = list(self.tree.item(str(row_id), "values"))
                    if len(values) >= 4:
                        values[3] = "正在翻译…"
                        self.tree.item(str(row_id), values=values)
        elif kind == "translation_stream":
            row_id = int(event.get("id", 0) or 0)
            if row_id and row_id == self.current_row_id:
                self.overlay.show_streamed_translation(
                    str(event.get("translation", ""))
                )
                self.translation_status_var.set(
                    f"当前句正在流式返回（{event.get('model', '')}）"
                )
        elif kind == "translation_partial":
            items = [item for item in event.get("items", []) if isinstance(item, dict)]
            ids = [int(item.get("id", 0) or 0) for item in items]
            row_details = {
                int(item["id"]): item
                for item in self.store.translation_items(ids)
            }
            for item in items:
                row_id = int(item.get("id", 0) or 0)
                translated = normalize_text(str(item.get("translation", "")))
                if row_id and self.tree.exists(str(row_id)):
                    values = list(self.tree.item(str(row_id), "values"))
                    if len(values) >= 4:
                        detail = row_details.get(row_id)
                        if detail:
                            values[1] = self.display_speaker(
                                str(detail.get("quest", "")),
                                str(detail.get("speaker", "")),
                            )
                        values[3] = translated.replace("\n", " ⏎ ")
                        self.tree.item(str(row_id), values=values)
            current_changed = self.current_row_id is not None and self.current_row_id in ids
            if current_changed:
                self._show_current_row(int(self.current_row_id))
                if self.log_window.is_visible():
                    self.log_window.refresh()
            if event.get("profile") == "preload":
                self.translation_status_var.set(
                    "完整剧情正在流式翻译："
                    f"已落库 {int(event.get('completed_count', 0) or 0)}/"
                    f"{int(event.get('requested_count', 0) or 0)} 条"
                )
            elif current_changed:
                self.translation_status_var.set(
                    f"当前句译文已返回（{event.get('model', '')}）"
                )
        elif kind == "translation_complete":
            items = [item for item in event.get("items", []) if isinstance(item, dict)]
            row_details = {
                int(item["id"]): item
                for item in self.store.translation_items(
                    [int(value.get("id", 0) or 0) for value in items]
                )
            }
            for item in items:
                row_id = int(item.get("id", 0) or 0)
                translated = normalize_text(str(item.get("translation", "")))
                if row_id and self.tree.exists(str(row_id)):
                    values = list(self.tree.item(str(row_id), "values"))
                    if len(values) >= 4:
                        detail = row_details.get(row_id)
                        if detail:
                            values[1] = self.display_speaker(
                                str(detail.get("quest", "")),
                                str(detail.get("speaker", "")),
                            )
                        values[3] = translated.replace("\n", " ⏎ ")
                        self.tree.item(str(row_id), values=values)
            # Hidden preload records are not present in the bilingual LOG.
            # Rebuilding the entire LOG (and formerly the entire 5k+ row main
            # table) for a 100-line Sol batch can stall Tk for seconds exactly
            # when the player advances. Refresh only if played rows changed.
            played_changed = any(
                not bool(row_details.get(int(item.get("id", 0) or 0), {}).get("preloaded", True))
                for item in items
            )
            if self.log_window.is_visible() and played_changed:
                self.log_window.refresh()
            if self.current_row_id is not None:
                self._show_current_row(self.current_row_id)
            failed = len(event.get("failed_ids", []))
            failed_ids = [int(value) for value in event.get("failed_ids", [])]
            if failed_ids and self.translator:
                # If an option-list response omitted an item, its selected row
                # is no longer forced to wait; translate that row as fallback.
                self.translator.enqueue(self.store.choice_dependents(failed_ids), urgent=True)
            suffix = f"；{failed} 条需重试" if failed else ""
            requested = int(event.get("requested_count", len(items)) or len(items))
            if event.get("profile") == "preload":
                summary = f"完整剧情本批已翻译 {len(items)}/{requested} 条"
            else:
                summary = f"AI 已翻译 {len(items)} 条"
            self.translation_status_var.set(
                f"{summary}（{event.get('model', '')}）{suffix}"
            )
        elif kind == "translation_preempted":
            self.translation_status_var.set(str(event.get("text", "已优先处理当前句")))
        elif kind == "translation_error":
            for row_id in event.get("ids", []):
                if self.tree.exists(str(row_id)):
                    values = list(self.tree.item(str(row_id), "values"))
                    if len(values) >= 4:
                        values[3] = "⚠ 翻译失败，可点击“重试未翻译”"
                        self.tree.item(str(row_id), values=values)
            if self.log_window.is_visible():
                self.log_window.refresh()
            if self.translator:
                failed_ids = [int(value) for value in event.get("ids", [])]
                self.translator.enqueue(
                    self.store.choice_dependents(failed_ids), urgent=True
                )
            self.translation_status_var.set(f"AI 翻译失败：{event.get('message', '')}")
        elif kind == "worker_error":
            self.status_var.set(str(event.get("message", "监听失败")))
            self.status_dot.configure(fg="#b23a3a")
        elif kind == "detached":
            self.status_var.set("FGO 已退出或重启，等待重新连接…")
            self.status_dot.configure(fg="#d18b00")
        elif kind == "diagnostic" and event.get("level") == "error":
            self.status_var.set(f"Hook 错误：{event.get('message', '')}")
            self.status_dot.configure(fg="#b23a3a")
        elif kind == "diagnostic" and event.get("level") == "warning":
            self.status_var.set(f"警告：{event.get('message', '')}")
            self.status_dot.configure(fg="#d18b00")

    def _selected_rows(self) -> list[tuple[str, str, str, str]]:
        selected = {int(item) for item in self.tree.selection() if str(item).isdigit()}
        return [
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
            for row in self.store.view_rows(include_preloaded=True)
            if int(row[0]) in selected
        ]

    def _all_rows(self) -> list[tuple[str, str, str, str]]:
        selected = self.quest_filter.get()
        quest = None if selected == "全部关卡" else selected
        return [
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
            for row in self.store.view_rows(
                quest, include_preloaded=self.show_preloaded_rows.get()
            )
        ]

    def _copy_rows(self, rows: list[tuple[str, str, str, str]]) -> None:
        if not rows:
            self.bell()
            return
        value = "\n\n".join(
            format_bilingual_entry(
                self.display_speaker(quest, speaker), text, translation
            )
            for quest, speaker, text, translation in rows
        )
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.status_var.set(f"已复制 {len(rows)} 条文本")

    def copy_selected(self) -> None:
        self._copy_rows(self._selected_rows())

    def copy_all(self) -> None:
        self._copy_rows(self._all_rows())

    def export_text(self) -> None:
        rows = self._all_rows()
        if not rows:
            self.bell()
            return
        default = f"FGO剧情_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path = filedialog.asksaveasfilename(
            title="导出剧情文本",
            defaultextension=".txt",
            initialfile=default,
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        value = "\n\n".join(
            format_bilingual_entry(
                self.display_speaker(quest, speaker), text, translation
            )
            for quest, speaker, text, translation in rows
        )
        Path(path).write_text(value, encoding="utf-8-sig")
        self.status_var.set(f"已导出 {len(rows)} 条：{path}")

    def clear_history(self) -> None:
        if not messagebox.askyesno("清空历史", "确定删除全部剧情历史记录吗？此操作不可撤销。"):
            return
        self.store.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._update_count()
        self._refresh_filter_values()
        self.quest_filter.set("全部关卡")
        if self.log_window.is_visible():
            self.log_window.refresh()
        self.status_var.set("历史记录已清空")

    def on_close(self) -> None:
        self.knowledge_cancel.set()
        if self.explanation_worker:
            self.explanation_worker.close()
        if self.translator:
            self.translator.close()
        if self.preload_translator:
            self.preload_translator.close()
        if self.worker:
            self.worker.stop()
        self.store.close()
        self.destroy()


def main() -> None:
    app = Application()
    app.mainloop()


if __name__ == "__main__":
    main()
