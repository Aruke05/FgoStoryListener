from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import re
import tempfile
import threading
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 3
MOONCELL_API = "https://fgo.wiki/api.php"
ATLAS_INFO = "https://api.atlasacademy.io/info"
ATLAS_BASIC_JP = "https://api.atlasacademy.io/export/JP/basic_servant.json"
ATLAS_BASIC_CN = "https://api.atlasacademy.io/export/CN/basic_servant.json"
USER_AGENT = "FgoStoryListener/2.6.0 (terminology and speech-style updater; local desktop app)"
MAX_SOURCE_RESPONSE_BYTES = 32 * 1024 * 1024

ProgressCallback = Callable[[str, int, int, str], None]


class KnowledgeUpdateError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _term_id(kind: str, ja: str, zh: str) -> str:
    raw = f"{kind}\0{ja}\0{zh}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def _style_id(speaker: str, ja_pattern: str, zh_rendering: str) -> str:
    raw = f"speech_style\0{speaker}\0{ja_pattern}\0{zh_rendering}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def _normalize_for_match(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def _clean_term(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<!--[\s\S]*?-->", "", text)
    text = re.sub(
        r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]",
        lambda match: match.group(1),
        text,
    )
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_safe_pair(ja: str, zh: str) -> bool:
    if not ja or not zh or ja in {"-", "?", "？"} or zh in {"-", "?", "？"}:
        return False
    if len(ja) > 120 or len(zh) > 120 or "\n" in ja or "\n" in zh:
        return False
    forbidden = ("{{", "}}", "[[", "]]", "<!--", "|", "=")
    if any(token in ja or token in zh for token in forbidden):
        return False
    return _normalize_for_match(ja) != _normalize_for_match(zh)


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    braces = 0
    brackets = 0
    index = 0
    while index < len(value):
        pair = value[index : index + 2]
        if pair == "{{":
            braces += 1
            index += 2
            continue
        if pair == "}}" and braces:
            braces -= 1
            index += 2
            continue
        if pair == "[[":
            brackets += 1
            index += 2
            continue
        if pair == "]]" and brackets:
            brackets -= 1
            index += 2
            continue
        if value[index] == "|" and braces == 0 and brackets == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    return parts


def extract_templates(wikitext: str, template_name: str) -> Iterable[str]:
    pattern = re.compile(r"\{\{\s*" + re.escape(template_name) + r"(?=\s*[|}])")
    for match in pattern.finditer(wikitext):
        depth = 0
        index = match.start()
        end = None
        while index < len(wikitext) - 1:
            pair = wikitext[index : index + 2]
            if pair == "{{":
                depth += 1
                index += 2
                continue
            if pair == "}}":
                depth -= 1
                index += 2
                if depth == 0:
                    end = index
                    break
                continue
            index += 1
        if end is not None:
            yield wikitext[match.start() : end]


def template_parts(template: str) -> tuple[dict[str, str], list[str]]:
    body = template[2:-2]
    pieces = _split_top_level(body)
    keyed: dict[str, str] = {}
    positional: list[str] = []
    for piece in pieces[1:]:
        if "=" in piece:
            key, value = piece.split("=", 1)
            key = key.strip()
            if key:
                keyed[key] = value.strip()
                continue
        positional.append(piece.strip())
    return keyed, positional


def _source_url(title: str) -> str:
    return "https://fgo.wiki/w/" + urllib.parse.quote(title.replace(" ", "_"), safe="()－·〔〕")


def _candidate(
    ja: Any,
    zh: Any,
    *,
    kind: str,
    scope: str,
    priority: int,
    status: str,
    source_id: str,
    source_url: str,
    source_title: str,
    source_revision: str = "",
    source_updated_at: str = "",
) -> dict[str, Any] | None:
    ja_text = _clean_term(ja)
    zh_text = _clean_term(zh)
    if not _is_safe_pair(ja_text, zh_text):
        return None
    return {
        "id": _term_id(kind, ja_text, zh_text),
        "ja": ja_text,
        "zh": zh_text,
        "aliases_ja": [],
        "aliases_zh": [],
        "kind": kind,
        "scope": scope,
        "priority": int(priority),
        "status": status,
        "source_id": source_id,
        "source_url": source_url,
        "source_title": source_title,
        "source_revision": str(source_revision or ""),
        "source_updated_at": str(source_updated_at or ""),
    }


def normalize_term(term: dict[str, Any], source: str = "bundled") -> dict[str, Any] | None:
    ja = _clean_term(term.get("ja", ""))
    zh = _clean_term(term.get("zh", ""))
    if not _is_safe_pair(ja, zh):
        return None
    kind = str(term.get("kind") or ("world_term" if term.get("scope") == "global" else "character"))
    normalized = {
        "id": str(term.get("id") or _term_id(kind, ja, zh)),
        "ja": ja,
        "zh": zh,
        "aliases_ja": [
            _clean_term(item) for item in term.get("aliases_ja", []) if _clean_term(item)
        ],
        "aliases_zh": [
            _clean_term(item) for item in term.get("aliases_zh", []) if _clean_term(item)
        ],
        "kind": kind,
        "scope": str(term.get("scope") or "global"),
        "priority": int(term.get("priority", 100 if source == "bundled" else 70)),
        "status": str(term.get("status") or ("curated" if source == "bundled" else "verified")),
        "source_id": str(term.get("source_id") or source),
        "source_url": str(term.get("source_url") or ""),
        "source_title": str(term.get("source_title") or source),
        "source_revision": str(term.get("source_revision") or ""),
        "source_updated_at": str(term.get("source_updated_at") or ""),
    }
    return normalized


def normalize_style(style: dict[str, Any], source: str = "bundled") -> dict[str, Any] | None:
    speaker = _clean_term(style.get("speaker", ""))
    pattern = _clean_term(style.get("ja_pattern", ""))
    rendering = _clean_term(style.get("zh_rendering", ""))
    if not speaker or not pattern or not rendering:
        return None
    if len(speaker) > 80 or len(pattern) > 16 or len(rendering) > 12:
        return None
    return {
        "id": str(style.get("id") or _style_id(speaker, pattern, rendering)),
        "speaker": speaker,
        "speaker_aliases": [
            _clean_term(item) for item in style.get("speaker_aliases", []) if _clean_term(item)
        ],
        "ja_pattern": pattern,
        "zh_rendering": rendering,
        "support": int(style.get("support", 0) or 0),
        "confidence": float(style.get("confidence", 1.0) or 0.0),
        "priority": int(style.get("priority", 110 if source == "bundled" else 80)),
        "status": str(style.get("status") or ("curated" if source == "bundled" else "verified")),
        "source_id": str(style.get("source_id") or source),
        "source_url": str(style.get("source_url") or ""),
        "source_title": str(style.get("source_title") or source),
        "source_revision": str(style.get("source_revision") or ""),
        "source_updated_at": str(style.get("source_updated_at") or ""),
    }


class KnowledgeBase:
    """Versioned terminology store with bundled fallback and atomic rollback."""

    def __init__(self, root: Path, bundled_path: Path):
        self.root = Path(root)
        self.bundled_path = Path(bundled_path)
        self.active_path = self.root / "active.json"
        self.previous_path = self.root / "previous.json"
        self.manifest_path = self.root / "manifest.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.document: dict[str, Any] = {}
        self.refresh()

    def _read_document(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        if not isinstance(value, dict) or not isinstance(value.get("terms"), list):
            return {}
        terms = [normalize_term(item, "bundled" if path == self.bundled_path else "active")
                 for item in value["terms"] if isinstance(item, dict)]
        terms = [item for item in terms if item]
        if not terms:
            return {}
        result = dict(value)
        result["terms"] = terms
        styles = [
            normalize_style(item, "bundled" if path == self.bundled_path else "active")
            for item in value.get("styles", [])
            if isinstance(item, dict)
        ]
        result["styles"] = [item for item in styles if item]
        return result

    def refresh(self) -> dict[str, Any]:
        active = self._read_document(self.active_path)
        bundled = self._read_document(self.bundled_path)
        self.document = active or bundled
        if active and bundled:
            # A user's last downloaded database may predate newly shipped
            # curated name/style rules. Overlay only protected high-priority
            # bundled entries in memory; ordinary downloaded knowledge remains
            # versioned and untouched on disk.
            terms = [item for item in active.get("terms", []) if isinstance(item, dict)]
            for item in bundled.get("terms", []):
                if not isinstance(item, dict) or not (
                    str(item.get("status", "")) in {"curated", "manual"}
                    or int(item.get("priority", 0) or 0) >= 100
                ):
                    continue
                keys = {
                    _normalize_for_match(str(item.get("ja", ""))),
                    *(
                        _normalize_for_match(str(alias))
                        for alias in item.get("aliases_ja", [])
                    ),
                }
                keys.discard("")
                terms = [
                    old for old in terms
                    if _normalize_for_match(str(old.get("ja", ""))) not in keys
                ]
                terms.append(item)
            styles = [item for item in active.get("styles", []) if isinstance(item, dict)]
            for item in bundled.get("styles", []):
                if not isinstance(item, dict) or not (
                    str(item.get("status", "")) in {"curated", "manual"}
                    or int(item.get("priority", 0) or 0) >= 100
                ):
                    continue
                key = (
                    _normalize_for_match(str(item.get("speaker", ""))),
                    _normalize_for_match(str(item.get("ja_pattern", ""))),
                )
                styles = [
                    old for old in styles
                    if (
                        _normalize_for_match(str(old.get("speaker", ""))),
                        _normalize_for_match(str(old.get("ja_pattern", ""))),
                    ) != key
                ]
                styles.append(item)
            self.document = dict(active)
            self.document["terms"] = terms
            self.document["styles"] = styles
            stats = dict(self.document.get("stats", {}))
            stats["active_terms"] = len(terms)
            stats["active_styles"] = len(styles)
            self.document["stats"] = stats
        return self.document

    def bundled_document(self) -> dict[str, Any]:
        return self._read_document(self.bundled_path)

    def summary(self) -> dict[str, Any]:
        stats = self.document.get("stats", {}) if isinstance(self.document, dict) else {}
        return {
            "version": str(self.document.get("knowledge_version") or self.document.get("version") or "内置"),
            "generated_at": str(self.document.get("generated_at") or ""),
            "term_count": int(stats.get("active_terms") or len(self.document.get("terms", []))),
            "style_count": int(stats.get("active_styles") or len(self.document.get("styles", []))),
            "source_count": len(self.document.get("sources", [])),
            "using_runtime": self.active_path.is_file() and bool(self._read_document(self.active_path)),
            "can_rollback": self.previous_path.is_file() and bool(self._read_document(self.previous_path)),
        }

    def relevant_terms(
        self,
        lines: list[dict[str, Any]],
        context: list[dict[str, Any]] | None = None,
        max_terms: int = 120,
    ) -> list[dict[str, Any]]:
        source_values: list[str] = []
        for item in [*lines, *(context or [])]:
            if not isinstance(item, dict):
                continue
            source_values.extend(
                str(item.get(key, "")) for key in ("speaker", "ja", "text")
            )
        haystack = _normalize_for_match("\n".join(source_values))
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for term in self.document.get("terms", []):
            if not isinstance(term, dict):
                continue
            keys = [str(term.get("ja", "")), *term.get("aliases_ja", [])]
            matches = [key for key in keys if key and _normalize_for_match(key) in haystack]
            if not matches:
                continue
            longest = max((len(value) for value in matches), default=0)
            score = (10000 if matches else 0) + longest * 20 + int(term.get("priority", 0))
            ranked.append((score, longest, term))
        ranked.sort(key=lambda value: (value[0], value[1]), reverse=True)
        output: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for _score, _length, term in ranked:
            key = (str(term.get("ja", "")), str(term.get("zh", "")))
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "ja": key[0],
                    "zh": key[1],
                    "kind": str(term.get("kind", "term")),
                    "scope": str(term.get("scope", "global")),
                }
            )
            if len(output) >= max_terms:
                break
        return output

    def translate_speaker(self, speaker: str) -> str:
        """Return only an exact, source-backed simplified-Chinese name.

        Speaker labels may be short nicknames, so substring/fuzzy matching is
        deliberately forbidden here. Unknown names are left for the model's
        per-quest cache rather than pretending a similarly named servant is the
        same character.
        """
        wanted = _normalize_for_match(_clean_term(speaker))
        if not wanted or (speaker.startswith("【") and speaker.endswith("】")):
            return ""
        allowed_kinds = {"character", "servant", "battle_name"}
        ranked: list[tuple[int, int, str]] = []
        for term in self.document.get("terms", []):
            if not isinstance(term, dict) or str(term.get("kind", "")) not in allowed_kinds:
                continue
            keys = [str(term.get("ja", "")), *term.get("aliases_ja", [])]
            if wanted not in {_normalize_for_match(value) for value in keys if value}:
                continue
            translated = _clean_term(term.get("zh", ""))
            if not translated:
                continue
            kind_rank = 2 if str(term.get("kind")) in {"character", "servant"} else 1
            ranked.append((kind_rank, int(term.get("priority", 0) or 0), translated))
        if not ranked:
            return ""
        ranked.sort(reverse=True)
        return ranked[0][2]

    def relevant_styles(
        self, lines: list[dict[str, Any]], max_styles: int = 24
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for line in lines:
            if not isinstance(line, dict):
                continue
            speaker = _normalize_for_match(str(line.get("speaker", "")))
            japanese = _normalize_for_match(str(line.get("ja", line.get("text", ""))))
            for style in self.document.get("styles", []):
                if not isinstance(style, dict):
                    continue
                speakers = [str(style.get("speaker", "")), *style.get("speaker_aliases", [])]
                if speaker not in {_normalize_for_match(value) for value in speakers if value}:
                    continue
                pattern = str(style.get("ja_pattern", ""))
                if not pattern or _normalize_for_match(pattern) not in japanese:
                    continue
                key = (
                    str(style.get("speaker", "")), pattern,
                    str(style.get("zh_rendering", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                selected.append(
                    {
                        "speaker": key[0],
                        "source_marker": key[1],
                        "required_zh_rendering": key[2],
                        "status": str(style.get("status", "verified")),
                    }
                )
                if len(selected) >= max_styles:
                    return selected
        return selected

    def rollback(self) -> dict[str, Any]:
        current = self._read_document(self.active_path)
        previous = self._read_document(self.previous_path)
        if not previous:
            raise KnowledgeUpdateError("没有可回滚的上一版术语库。")
        old_active = self.active_path.read_bytes() if current else b""
        _atomic_write_json(self.active_path, previous)
        if old_active:
            _atomic_write(self.previous_path, old_active)
        self.refresh()
        return self.summary()


class KnowledgeUpdater:
    """Build a source-backed FGO lexicon and promote only validated output."""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        app_version: str,
        progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.knowledge = knowledge
        self.app_version = app_version
        self.progress = progress or (lambda _stage, _current, _total, _text: None)
        self.cancel_event = cancel_event or threading.Event()
        self._snapshot: dict[str, Any] = {}

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise KnowledgeUpdateError("术语库更新已取消。")

    def _json_request(
        self, url: str, params: dict[str, Any] | None = None, post: bool = False
    ) -> tuple[Any, str]:
        self._check_cancelled()
        values = params or {}
        data = urllib.parse.urlencode(values).encode("utf-8") if post else None
        if values and not post:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(values)
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(MAX_SOURCE_RESPONSE_BYTES + 1)
        except Exception as exc:
            raise KnowledgeUpdateError(f"下载术语来源失败：{url}\n{exc}") from exc
        if len(raw) > MAX_SOURCE_RESPONSE_BYTES:
            raise KnowledgeUpdateError(f"术语来源响应超过 32 MiB 安全上限：{url}")
        try:
            return json.loads(raw), _sha256(raw)
        except ValueError as exc:
            raise KnowledgeUpdateError(f"术语来源不是有效 JSON：{url}") from exc

    def _mooncell_api(self, params: dict[str, Any], post: bool = False) -> tuple[dict[str, Any], str]:
        values = {"format": "json", "formatversion": "2", **params}
        value, digest = self._json_request(MOONCELL_API, values, post)
        if not isinstance(value, dict) or value.get("error"):
            raise KnowledgeUpdateError(f"Mooncell API 返回错误：{value.get('error', value)}")
        return value, digest

    def _category_members(self, category: str) -> list[str]:
        titles: list[str] = []
        continuation: dict[str, Any] = {}
        while True:
            value, _digest = self._mooncell_api(
                {
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": f"分类:{category}",
                    "cmnamespace": "0",
                    "cmtype": "page",
                    "cmlimit": "max",
                    **continuation,
                }
            )
            titles.extend(
                str(item.get("title", ""))
                for item in value.get("query", {}).get("categorymembers", [])
                if item.get("title")
            )
            if len(titles) > 5000:
                raise KnowledgeUpdateError(f"Mooncell 分类“{category}”超过 5000 页安全上限。")
            if "continue" not in value:
                break
            continuation = value["continue"]
        return list(dict.fromkeys(titles))

    def _fetch_pages(self, titles: list[str], stage: str) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        total = len(titles)
        for offset in range(0, total, 30):
            self._check_cancelled()
            batch = titles[offset : offset + 30]
            value, _digest = self._mooncell_api(
                {
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "ids|timestamp|content",
                    "rvslots": "main",
                    "redirects": "1",
                    "titles": "|".join(batch),
                },
                post=True,
            )
            for page in value.get("query", {}).get("pages", []):
                revisions = page.get("revisions", [])
                if not revisions:
                    continue
                revision = revisions[0]
                content = revision.get("slots", {}).get("main", {}).get("content", "")
                if not isinstance(content, str):
                    continue
                pages.append(
                    {
                        "title": str(page.get("title", "")),
                        "pageid": int(page.get("pageid", 0) or 0),
                        "revid": int(revision.get("revid", 0) or 0),
                        "timestamp": str(revision.get("timestamp", "")),
                        "wikitext": content,
                    }
                )
            self.progress(stage, min(offset + len(batch), total), total, f"已读取 {min(offset + len(batch), total)}/{total} 页")
        return pages

    @staticmethod
    def _page_candidate(
        page: dict[str, Any], ja: Any, zh: Any, kind: str, priority: int, status: str
    ) -> dict[str, Any] | None:
        title = str(page.get("title", ""))
        return _candidate(
            ja,
            zh,
            kind=kind,
            scope="global",
            priority=priority,
            status=status,
            source_id="mooncell",
            source_url=_source_url(title),
            source_title=title,
            source_revision=str(page.get("revid", "")),
            source_updated_at=str(page.get("timestamp", "")),
        )

    def _parse_servant_page(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        text = str(page.get("wikitext", ""))
        for template in extract_templates(text, "基础数值"):
            keyed, _positional = template_parts(template)
            for zh_prefix, ja_prefix, kind in (
                ("中文名", "日文名", "servant"),
                ("中文战斗名", "日文战斗名", "battle_name"),
                ("中文卡面名", "日文卡面名", "card_name"),
            ):
                suffixes = {
                    key[len(zh_prefix) :]
                    for key in keyed
                    if key == zh_prefix or re.fullmatch(re.escape(zh_prefix) + r"\d+", key)
                }
                for suffix in suffixes:
                    candidate = self._page_candidate(
                        page,
                        keyed.get(ja_prefix + suffix, ""),
                        keyed.get(zh_prefix + suffix, ""),
                        kind,
                        92,
                        "community_verified",
                    )
                    if candidate:
                        results.append(candidate)
        for template in extract_templates(text, "宝具"):
            keyed, _positional = template_parts(template)
            for zh_key, ja_key, kind in (
                ("中文名", "日文名", "noble_phantasm"),
                ("国服上标", "日服上标", "noble_phantasm_reading"),
            ):
                candidate = self._page_candidate(
                    page, keyed.get(ja_key, ""), keyed.get(zh_key, ""), kind, 76,
                    "community_verified",
                )
                if candidate:
                    results.append(candidate)
        for template in extract_templates(text, "持有技能"):
            _keyed, positional = template_parts(template)
            if len(positional) >= 3:
                candidate = self._page_candidate(
                    page, positional[2], positional[1], "skill", 74, "community_verified"
                )
                if candidate:
                    results.append(candidate)
        return results

    def _parse_event_page(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for template in extract_templates(str(page.get("wikitext", "")), "活动信息"):
            keyed, _positional = template_parts(template)
            chinese = keyed.get("名称cn") or keyed.get("名称ha")
            priority = 88 if keyed.get("名称cn") else 65
            status = "official_cn" if keyed.get("名称cn") else "community_verified"
            candidate = self._page_candidate(
                page, keyed.get("名称jp", ""), chinese, "event", priority, status
            )
            if candidate:
                results.append(candidate)
        return results

    @staticmethod
    def _clean_voice_value(value: Any) -> str:
        text = str(value or "")
        text = re.sub(r"<ref\b[^>]*>[\s\S]*?</ref>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<ref\b[^>]*/>", "", text, flags=re.IGNORECASE)
        # Preserve the visible payload of common one-argument presentation wrappers.
        text = re.sub(r"\{\{(?:黑幕|ruby)\|([^{}|]+)(?:\|[^{}]*)?\}\}", r"\1", text)
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
        return _clean_term(text)

    def _parse_voice_styles(
        self, page: dict[str, Any], speaker: str, aliases: list[str]
    ) -> list[dict[str, Any]]:
        """Infer only strongly repeated speaker-specific ending mappings."""
        pairs: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for template in extract_templates(str(page.get("wikitext", "")), "#invoke:VoiceTable"):
            keyed, _positional = template_parts(template)
            suffixes = {
                match.group(1)
                for key in keyed
                if (match := re.fullmatch(r"日文(\d+)", key))
            }
            for suffix in suffixes:
                ja = self._clean_voice_value(keyed.get("日文" + suffix, ""))
                zh = self._clean_voice_value(keyed.get("中文" + suffix, ""))
                if ja and zh and (ja, zh) not in seen_pairs:
                    seen_pairs.add((ja, zh))
                    pairs.append((ja, zh))
        if len(pairs) < 8:
            return []

        common_ja = {
            "です", "ます", "でした", "ません", "でしょう", "ましょう", "ください",
            "だよ", "だね", "だな", "なの", "ので", "から", "けど", "かな", "よね",
            "いる", "ある", "ない", "たい", "れる", "られる", "する", "した", "して",
        }
        common_zh = {
            "了", "吗", "呢", "吧", "啊", "呀", "哦", "啦", "的", "是", "我", "你",
            "好了", "不是", "可以", "什么", "这样", "那样",
        }
        # A source-backed global style rule is deliberately much stricter than
        # an ordinary glossary entry.  Only clearly stylised sentence-final
        # sounds are eligible; names (御主/维), laughter and normal particles
        # must never become hard translation constraints.
        distinctive_zh_endings = {
            "啾", "喵", "咪", "汪", "咩", "呱", "嘎", "嗷", "唧", "噗",
            "姆", "咕", "啵", "哞", "喏", "嘞", "呦", "唷", "哒",
        }
        # Only inspect a grammatical suffix.  The previous implementation made
        # every kana n-gram near the end a candidate, which could turn lexical
        # fragments such as ``これは`` into a supposed verbal tic.  Scenario
        # dialogue is intentionally not used as an unsupervised corpus here:
        # the source must already provide aligned Japanese/Chinese voice rows.
        final_particles = {"か", "ね", "よ", "な", "ぞ", "ぜ", "わ"}
        grammatical_starts = (
            "で", "ま", "だ", "な", "の", "に", "ご", "じゃ", "や",
            "デ", "マ", "ダ", "ナ", "ノ", "ニ", "ゴ", "ジャ", "ヤ", "ア",
        )
        ja_sets: dict[str, set[int]] = defaultdict(set)
        zh_sets: dict[str, set[int]] = defaultdict(set)
        for index, (ja, zh) in enumerate(pairs):
            for segment in re.split(r"[。！？!?…]+", ja):
                match = re.search(r"([ぁ-んァ-ヶー]{2,10})$", segment.strip())
                if not match:
                    continue
                tail = match.group(1)
                # A final question/emphasis particle is not part of the tic.
                # If present, analyse the suffix immediately before it instead.
                if len(tail) >= 3 and tail[-1] in final_particles:
                    tail = tail[:-1]
                for size in range(2, min(6, len(tail)) + 1):
                    marker = tail[-size:]
                    if marker not in common_ja:
                        ja_sets[marker].add(index)
            for marker in re.findall(r"([\u3400-\u9fff]{1,3})(?=\s*[。！？!?…]|$)", zh):
                for size in range(1, min(3, len(marker)) + 1):
                    candidate = marker[-size:]
                    if candidate not in common_zh and candidate in distinctive_zh_endings:
                        zh_sets[candidate].add(index)

        candidates: list[dict[str, Any]] = []
        title = str(page.get("title", ""))
        for ja_marker, ja_indexes in ja_sets.items():
            if not re.search(r"[ぁ-ん]", ja_marker):
                continue
            if re.fullmatch(r"[はふへほぁあうえおっー]+", ja_marker):
                continue
            if len(ja_indexes) < 5:
                continue
            matches: list[tuple[float, int, int, str]] = []
            for zh_marker, zh_indexes in zh_sets.items():
                overlap = len(ja_indexes & zh_indexes)
                confidence = overlap / len(ja_indexes)
                reverse_confidence = overlap / len(zh_indexes)
                if overlap >= 5 and confidence >= 0.80 and reverse_confidence >= 0.75:
                    matches.append((confidence, overlap, -len(zh_marker), zh_marker))
            if not matches:
                continue
            confidence, support, _negative_length, zh_marker = max(matches)
            candidates.append(
                {
                    "id": _style_id(speaker, ja_marker, zh_marker),
                    "speaker": speaker,
                    "speaker_aliases": list(dict.fromkeys(aliases)),
                    "ja_pattern": ja_marker,
                    "zh_rendering": zh_marker,
                    "support": support,
                    "confidence": round(confidence, 4),
                    "priority": 82,
                    "status": "voice_corpus_verified",
                    "source_id": "mooncell-voice",
                    "source_url": _source_url(title),
                    "source_title": title,
                    "source_revision": str(page.get("revid", "")),
                    "source_updated_at": str(page.get("timestamp", "")),
                }
            )

        # Prefer broad, highly-supported markers and suppress redundant
        # substrings mapping to the same rendering.
        candidates.sort(
            key=lambda item: (
                int(item["support"]),
                float(item["confidence"]),
                str(item["ja_pattern"]).startswith(grammatical_starts),
                # Once support is equal, prefer a complete grammatical form
                # such as まちゅ rather than its inner fragment ちゅ.
                len(str(item["ja_pattern"])),
                -len(str(item["zh_rendering"])),
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        for item in candidates:
            if any(
                item["zh_rendering"] == old["zh_rendering"]
                and (
                    str(item["ja_pattern"]).endswith(str(old["ja_pattern"]))
                    or str(old["ja_pattern"]).endswith(str(item["ja_pattern"]))
                )
                for old in selected
            ):
                continue
            selected.append(item)
            if len(selected) >= 8:
                break
        return selected

    def _atlas_terms(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        self.progress("atlas", 0, 3, "正在读取 Atlas Academy 游戏数据版本")
        info, info_hash = self._json_request(ATLAS_INFO)
        self.progress("atlas", 1, 3, "正在读取日服从者数据")
        jp, jp_hash = self._json_request(ATLAS_BASIC_JP)
        self.progress("atlas", 2, 3, "正在读取国服从者数据")
        cn, cn_hash = self._json_request(ATLAS_BASIC_CN)
        if not isinstance(jp, list) or not isinstance(cn, list):
            raise KnowledgeUpdateError("Atlas Academy 从者数据格式异常。")
        jp_by_no = {int(item.get("collectionNo", 0)): item for item in jp if int(item.get("collectionNo", 0) or 0) > 0}
        cn_by_no = {int(item.get("collectionNo", 0)): item for item in cn if int(item.get("collectionNo", 0) or 0) > 0}
        terms: list[dict[str, Any]] = []
        untranslated: list[dict[str, Any]] = []
        for collection_no, jp_item in jp_by_no.items():
            cn_item = cn_by_no.get(collection_no)
            if not cn_item:
                untranslated.append(
                    {
                        "kind": "servant",
                        "atlas_collection_no": collection_no,
                        "ja": _clean_term(jp_item.get("name", "")),
                        "reason": "日服已收录、国服游戏数据尚无对应条目",
                    }
                )
                continue
            candidate = _candidate(
                jp_item.get("name", ""), cn_item.get("name", ""),
                kind="servant", scope="global", priority=85, status="official_cn",
                source_id="atlas-jp-cn", source_url=ATLAS_BASIC_CN,
                source_title=f"Atlas Academy JP/CN basic_servant #{collection_no}",
                source_revision=str(info.get("CN", {}).get("hash", "")) if isinstance(info, dict) else "",
            )
            if candidate:
                terms.append(candidate)
            jp_costumes = jp_item.get("costume", {}) if isinstance(jp_item.get("costume"), dict) else {}
            cn_costumes = cn_item.get("costume", {}) if isinstance(cn_item.get("costume"), dict) else {}
            for costume_key, jp_costume in jp_costumes.items():
                cn_costume = cn_costumes.get(costume_key)
                if not isinstance(jp_costume, dict) or not isinstance(cn_costume, dict):
                    continue
                costume = _candidate(
                    jp_costume.get("shortName", ""), cn_costume.get("shortName", ""),
                    kind="costume", scope="global", priority=85, status="official_cn",
                    source_id="atlas-jp-cn", source_url=ATLAS_BASIC_CN,
                    source_title=f"Atlas Academy costume #{collection_no}/{costume_key}",
                    source_revision=str(info.get("CN", {}).get("hash", "")) if isinstance(info, dict) else "",
                )
                if costume:
                    terms.append(costume)
        sources = [
            {
                "id": "atlas-jp-cn",
                "name": "Atlas Academy FGO Game Data API",
                "url": "https://api.atlasacademy.io/",
                "retrieved_at": utc_now(),
                "upstream_version": info,
                "artifacts": {
                    "info_sha256": info_hash,
                    "jp_basic_servant_sha256": jp_hash,
                    "cn_basic_servant_sha256": cn_hash,
                },
                "role": "日服发现 + 国服官方名称配对",
            }
        ]
        self._snapshot["atlas"] = {"info": info, "jp_basic_servant": jp, "cn_basic_servant": cn}
        self.progress("atlas", 3, 3, f"Atlas 配对完成：{len(terms)} 个术语")
        return terms, untranslated, sources

    @staticmethod
    def _merge(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            groups[_normalize_for_match(str(item.get("ja", "")))].append(item)
        active: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for normalized_ja, items in groups.items():
            by_translation: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for item in items:
                by_translation[_normalize_for_match(str(item.get("zh", "")))].append(item)
            ranked = sorted(
                ((max(int(item.get("priority", 0)) for item in values), key, values)
                 for key, values in by_translation.items()),
                reverse=True,
            )
            top_priority = ranked[0][0]
            top = [value for value in ranked if value[0] == top_priority]
            if len(top) > 1:
                conflicts.append(
                    {
                        "ja_normalized": normalized_ja,
                        "reason": "同优先级来源给出不同中文译名，未自动写入活动术语库",
                        "candidates": items,
                    }
                )
                continue
            winner_values = top[0][2]
            winner = sorted(
                winner_values,
                key=lambda item: (int(item.get("priority", 0)), len(str(item.get("source_revision", "")))),
                reverse=True,
            )[0].copy()
            corroboration = []
            for item in items:
                marker = {
                    "source_id": item.get("source_id", ""),
                    "source_title": item.get("source_title", ""),
                    "source_revision": item.get("source_revision", ""),
                    "zh": item.get("zh", ""),
                    "priority": item.get("priority", 0),
                }
                if marker not in corroboration:
                    corroboration.append(marker)
            winner["corroboration"] = corroboration[:12]
            winner["id"] = _term_id(str(winner.get("kind", "term")), str(winner["ja"]), str(winner["zh"]))
            active.append(winner)
            if len(by_translation) > 1:
                conflicts.append(
                    {
                        "ja_normalized": normalized_ja,
                        "reason": "已按来源优先级选择；较低优先级译名仅保留在暂存区",
                        "selected": {"ja": winner["ja"], "zh": winner["zh"], "priority": winner["priority"]},
                        "candidates": items,
                    }
                )
        active.sort(key=lambda item: (str(item.get("kind", "")), str(item.get("ja", ""))))
        return active, conflicts

    @staticmethod
    def _merge_styles(
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in candidates:
            groups[
                (
                    _normalize_for_match(str(item.get("speaker", ""))),
                    _normalize_for_match(str(item.get("ja_pattern", ""))),
                )
            ].append(item)
        active: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for key, items in groups.items():
            ranked = sorted(
                items,
                key=lambda item: (
                    int(item.get("priority", 0)),
                    float(item.get("confidence", 0.0)),
                    int(item.get("support", 0)),
                ),
                reverse=True,
            )
            winner = ranked[0].copy()
            winner_score = (
                int(winner.get("priority", 0)),
                float(winner.get("confidence", 0.0)),
                int(winner.get("support", 0)),
            )
            tied = [
                item for item in ranked
                if (
                    int(item.get("priority", 0)),
                    float(item.get("confidence", 0.0)),
                    int(item.get("support", 0)),
                ) == winner_score
                and _normalize_for_match(str(item.get("zh_rendering", "")))
                != _normalize_for_match(str(winner.get("zh_rendering", "")))
            ]
            if tied:
                conflicts.append(
                    {
                        "speaker": key[0],
                        "ja_pattern": key[1],
                        "reason": "同置信度语音语料给出不同口癖译法，未自动启用",
                        "candidates": items,
                    }
                )
                continue
            winner["id"] = _style_id(
                str(winner["speaker"]), str(winner["ja_pattern"]),
                str(winner["zh_rendering"]),
            )
            active.append(winner)
            if len({_normalize_for_match(str(item.get("zh_rendering", ""))) for item in items}) > 1:
                conflicts.append(
                    {
                        "speaker": key[0],
                        "ja_pattern": key[1],
                        "reason": "已按人工优先级和语音语料置信度选择",
                        "selected": winner,
                        "candidates": items,
                    }
                )
        active.sort(key=lambda item: (str(item.get("speaker", "")), str(item.get("ja_pattern", ""))))
        return active, conflicts

    def update(self) -> dict[str, Any]:
        started = utc_now()
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.progress("start", 0, 1, "开始构建新版术语库（旧版仍保持可用）")
        bundled = self.knowledge.bundled_document()
        protected = [
            item
            for document in (bundled, self.knowledge.document)
            for item in document.get("terms", [])
            if isinstance(item, dict)
            and (
                str(item.get("status", "")) in {"curated", "manual"}
                or int(item.get("priority", 0) or 0) >= 100
            )
        ]
        manual_terms = [normalize_term(item, "bundled") for item in protected]
        manual_terms = [item for item in manual_terms if item]
        protected_styles = [
            item
            for document in (bundled, self.knowledge.document)
            for item in document.get("styles", [])
            if isinstance(item, dict)
            and (
                str(item.get("status", "")) in {"curated", "manual"}
                or int(item.get("priority", 0) or 0) >= 100
            )
        ]
        manual_styles = [normalize_style(item, "bundled") for item in protected_styles]
        manual_styles = [item for item in manual_styles if item]

        atlas_terms, untranslated, sources = self._atlas_terms()
        self.progress("mooncell", 0, 1, "正在读取 Mooncell 从者与活动目录")
        servant_titles = self._category_members("从者")
        event_titles = self._category_members("活动")
        if len(servant_titles) < 300:
            raise KnowledgeUpdateError(f"Mooncell 从者目录仅返回 {len(servant_titles)} 页，拒绝覆盖旧库。")
        servant_pages = self._fetch_pages(servant_titles, "mooncell-servants")
        event_pages = self._fetch_pages(event_titles, "mooncell-events")
        self.progress("mooncell-voices", 0, len(servant_titles), "正在读取 Mooncell 日中语音口癖语料")
        voice_pages = self._fetch_pages(
            [f"{title}/语音" for title in servant_titles], "mooncell-voices"
        )
        mooncell_terms: list[dict[str, Any]] = []
        for page in servant_pages:
            mooncell_terms.extend(self._parse_servant_page(page))
        for page in event_pages:
            mooncell_terms.extend(self._parse_event_page(page))
        japanese_name_by_title: dict[str, str] = {}
        for term in mooncell_terms:
            if str(term.get("kind", "")) != "servant":
                continue
            japanese_name_by_title[
                _normalize_for_match(str(term.get("source_title", "")))
            ] = str(term.get("ja", ""))
        voice_styles: list[dict[str, Any]] = []
        for page in voice_pages:
            base_title = re.sub(r"/语音$", "", str(page.get("title", "")))
            speaker = japanese_name_by_title.get(_normalize_for_match(base_title), base_title)
            aliases = [base_title] if speaker != base_title else []
            voice_styles.extend(self._parse_voice_styles(page, speaker, aliases))
        sources.append(
            {
                "id": "mooncell",
                "name": "Mooncell FGO Wiki",
                "url": "https://fgo.wiki",
                "retrieved_at": utc_now(),
                    "upstream_version": {
                        "servant_pages": len(servant_pages),
                        "event_pages": len(event_pages),
                        "voice_pages": len(voice_pages),
                    "max_revision": max(
                        [
                            int(page.get("revid", 0))
                            for page in servant_pages + event_pages + voice_pages
                        ],
                        default=0,
                    ),
                },
                "role": "结构化日中从者名、战斗名、宝具、技能、活动名与高置信度人物口癖",
            }
        )
        sources.extend(
            [
                {
                    "id": "fgo-official-jp",
                    "name": "Fate/Grand Order 日本官网",
                    "url": "https://www.fate-go.jp/",
                    "role": "作品与日服官方命名参考；不从自由文本自动抽取",
                },
                {
                    "id": "fgo-official-cn",
                    "name": "《命运-冠位指定》国服官网",
                    "url": "https://game.bilibili.com/fgo/",
                    "role": "国服官方核心术语参考；内置锁定词优先",
                },
            ]
        )
        self._snapshot["mooncell"] = {
            "servant_category_titles": servant_titles,
            "event_category_titles": event_titles,
            "pages": servant_pages + event_pages + voice_pages,
        }

        candidates = [*manual_terms, *atlas_terms, *mooncell_terms]
        active, conflicts = self._merge(candidates)
        styles, style_conflicts = self._merge_styles([*manual_styles, *voice_styles])
        servant_count = sum(1 for item in active if item.get("kind") == "servant")
        if len(active) < 500 or servant_count < 300 or len(active) > 20000:
            raise KnowledgeUpdateError(
                f"新版术语库校验未通过（有效术语 {len(active)}，从者 {servant_count}），旧版未改动。"
            )
        source_counts: dict[str, int] = defaultdict(int)
        kind_counts: dict[str, int] = defaultdict(int)
        for item in active:
            source_counts[str(item.get("source_id", "unknown"))] += 1
            kind_counts[str(item.get("kind", "term"))] += 1
        generated = utc_now()
        document = {
            "schema_version": SCHEMA_VERSION,
            "knowledge_version": version,
            "generated_at": generated,
            "generator": f"FgoStoryListener/{self.app_version}",
            "policy": "source-backed-versioned-atomic-promotion",
            "sources": sources,
            "stats": {
                "active_terms": len(active),
                "active_styles": len(styles),
                "candidate_terms": len(candidates),
                "candidate_styles": len(manual_styles) + len(voice_styles),
                "conflicts": len(conflicts),
                "style_conflicts": len(style_conflicts),
                "untranslated_discoveries": len(untranslated),
                "by_kind": dict(sorted(kind_counts.items())),
                "by_selected_source": dict(sorted(source_counts.items())),
            },
            "terms": active,
            "styles": styles,
        }
        staging = {
            "schema_version": SCHEMA_VERSION,
            "knowledge_version": version,
            "generated_at": generated,
            "conflicts": conflicts,
            "style_conflicts": style_conflicts,
            "untranslated_discoveries": untranslated,
        }
        # Persist evidence first. Promotion happens only after every validation and
        # artifact write succeeded, so an interrupted update leaves active.json intact.
        snapshot_bytes = _canonical_json(self._snapshot)
        snapshot_path = self.knowledge.root / "snapshots" / f"{version}.json.gz"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(snapshot_path, gzip.compress(snapshot_bytes, compresslevel=9))
        staging_path = self.knowledge.root / "staging" / f"{version}.json"
        _atomic_write_json(staging_path, staging)
        current = self.knowledge._read_document(self.knowledge.active_path)
        if not current:
            current = bundled
        if current:
            _atomic_write_json(self.knowledge.previous_path, current)
        _atomic_write_json(self.knowledge.active_path, document)

        manifest: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "history": []}
        try:
            old_manifest = json.loads(self.knowledge.manifest_path.read_text(encoding="utf-8"))
            if isinstance(old_manifest, dict):
                manifest = old_manifest
        except (OSError, ValueError):
            pass
        history = manifest.get("history", []) if isinstance(manifest.get("history"), list) else []
        history.append(
            {
                "knowledge_version": version,
                "started_at": started,
                "completed_at": generated,
                "active_sha256": _sha256(_canonical_json(document)),
                "snapshot": str(snapshot_path.name),
                "staging": str(staging_path.name),
                "stats": document["stats"],
            }
        )
        manifest["history"] = history[-20:]
        manifest["active_version"] = version
        _atomic_write_json(self.knowledge.manifest_path, manifest)
        self.knowledge.refresh()
        self.progress(
            "complete", 1, 1,
            f"知识库已更新：{len(active)} 个术语，{len(styles)} 条人物口癖规则",
        )
        return {
            **self.knowledge.summary(),
            "conflicts": len(conflicts),
            "style_conflicts": len(style_conflicts),
            "untranslated_discoveries": len(untranslated),
            "snapshot_path": str(snapshot_path),
            "staging_path": str(staging_path),
        }
