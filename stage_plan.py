"""Resolve JP scenario pages from public scripts, validated against observed visible slots."""
import hashlib
import json
import re
from collections import Counter
from functools import lru_cache
from urllib.request import Request, urlopen

SOURCE = "AtlasJP.VerifiedPages.v1"
VARIANT = re.compile(r"\[&([^:\[\]\n]+):([^:\[\]\n]+)\]")
NAME = re.compile(r"[ぁ-ヿ一-鿿々Ａ-Ｚａ-ｚA-Za-z０-９0-9・＆ ]{1,60}")
PAGE = re.compile(r"^＠([^\n]*)\n((?:(?![＠＄＊？?\[])[^\n]+\n)+)\[k\][ \t]*$", re.M)
BREAK = re.compile(r"(?im)^\s*(?:[＠＊？?＄]|\[(?:label|select\w*|branch\w*|goto\w*|jump\w*|if\w*)\b)")
def matches_prior(expected, observed):
    """Require adjacent played pages, including an exact materialized variant, not fuzzy text."""
    if not expected or len(expected) > len(observed):
        return False
    for item, (speaker, text) in zip(expected, observed[-len(expected):]):
        if item["speaker"] != speaker:
            return False
        parts = re.split(r"（候选：([^（）／]+)／([^（）／]+)）", item["ja"].replace("\n",""))
        pattern = re.escape(parts[0])
        for i in range(1,len(parts),3):
            pattern += "(?:" + re.escape(parts[i]) + "|" + re.escape(parts[i+1]) + ")" + re.escape(parts[i+2])
        if not re.fullmatch(pattern, text.replace("\n","")):
            return False
    return True

@lru_cache(maxsize=24)
def _get(url):
    with urlopen(Request(url, headers={"User-Agent": "FgoStoryListener/2.11.2-stage1"}), timeout=5) as r:
        data = r.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Script response too large")
    return data.decode("utf-8-sig")

def aligned_pages(raw, slots, normalize, sid):
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = [v["text"].replace("\n", "") for v in slots]
    combined = "".join(parts)
    bounds = {0}
    pos = 0
    for p in parts:
        pos += len(p)
        bounds.add(pos)
    candidates = []
    for m in PAGE.finditer(raw):
        speaker, body = m[1].strip(), normalize(m[2])
        # Explicit simple name plate only. No aliases, macros or inferred carry-over.
        if not NAME.fullmatch(speaker) or not body or len(body) > 2400:
            continue
        skeleton = VARIANT.sub("", body)
        if any(c in skeleton for c in "[]＠＄") or any(c in body for c in "{}"):
            continue
        if not re.fullmatch(r"[^\[\]]*(?:\[&[^:\[\]]+:[^:\[\]]+\][^\[\]]*)*", body):
            continue
        needle = skeleton.replace("\n", "")
        if not needle: continue
        i = combined.find(needle)
        if i < 0 or i not in bounds or i+len(needle) not in bounds or combined.find(needle, i+1) >= 0:
            continue
        candidates.append((i, i+len(needle), m, speaker, body, needle))
    counts = Counter(row[-1] for row in candidates)
    candidates = [r for r in candidates if counts[r[-1]] == 1]
    if any(a[1] > b[0] for a,b in zip(candidates,candidates[1:])):
        return []
    out, history = [], []
    prev = None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    for start,end,m,speaker,body,_ in candidates:
        if not prev or start != prev[0] or BREAK.search(raw[prev[1]:m.start()]):
            history = []
        dynamic = bool(VARIANT.search(body))
        if not dynamic:
            out.append(dict(speaker=speaker, text=body,
                            stage_key=f"{sid}:{digest}:{m.start()}",
                            prior=history[-2:]))
        history = (history + [dict(speaker=speaker, ja=VARIANT.sub(r"（候选：\1／\2）",body))])[-2:]
        prev = (end,m.end())
    return out

def resolve(quest, slots, normalize, fetch=_get):
    if not isinstance(quest,dict): return []
    q,p = str(quest.get("quest_id","")),str(quest.get("phase",""))
    if not re.fullmatch(r"[1-9]\d{0,9}",q) or not re.fullmatch(r"[1-9]\d?",p) or not slots: return []
    meta = json.loads(fetch(f"https://api.atlasacademy.io/nice/JP/quest/{q}/{p}"))
    if str(meta.get("id")) != q or str(meta.get("phase")) != p: return []
    ids = {str(s.get("scriptId")) for s in meta.get("scripts",[]) if isinstance(s,dict)}
    ids = {sid for sid in ids if re.fullmatch(r"[0-9]{8,12}",sid)}
    hint = str(quest.get("script") or "").rsplit("/",1)[-1].removesuffix(".txt").replace("-","")
    if hint.isascii() and hint.isdigit():
        ids &= {hint}
    if not 1 <= len(ids) <= 6: return []
    matches=[]
    for sid in sorted(ids):
        raw=fetch(f"https://static.atlasacademy.io/JP/Script/{sid[:2]}/{sid[:4]}/{sid}.txt")
        plan=aligned_pages(raw,slots,normalize,sid)
        if len(plan) >= 2:
            matches.append(plan)
    return matches[0] if len(matches)==1 else []
