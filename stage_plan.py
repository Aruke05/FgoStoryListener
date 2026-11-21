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
SLOT_NAME = re.compile(r"[A-ZＡ-Ｚ][：:]([^：:]+)")

def nameplate(header):
    """Only parse an explicit display name. A lone stage slot is not a name."""
    header = header.strip()
    tagged = SLOT_NAME.fullmatch(header)
    value = (tagged[1] if tagged else header).strip()
    if (not value or len(value)>60 or re.fullmatch(r"[A-ZＡ-Ｚ]",value)
            or not re.fullmatch(r"[\w々〆・＆！？ー、&'’〜～＝=／/（）() .\-]+",value)):
        return None
    return value, bool(tagged or not NAME.fullmatch(value))
PAGE = re.compile(r"^＠([^\n]*)\n((?:(?![＠＄＊？?]|\[k\])[^\n]+\n)+)\[k\][ \t]*$", re.M)
# Only directives with audited, presentation-only arguments. Unknown commands
# remain in the string and disqualify the page; never strip arbitrary tags.
FX = re.compile(r"\[(?:wt \d+(?:\.\d+)?|charaFace [A-Z] \d+|charaFaceFade [A-Z] \d+ \d+(?:\.\d+)?|charaMoveReturn [A-Z] -?\d+,-?\d+ \d+(?:\.\d+)?)\]")
SHAKE = re.compile(r"\[messageShake(?: \d+(?:\.\d+)?){4}\]")
# Candidate-only: numeric presentation commands in known visual/audio families.
# Never strip arbitrary tags, branch selectors, variable names or Japanese text.
PRESENTATION = re.compile(r"\[(?:camera[A-Z][A-Za-z]*|chara(?:Move|Fade|Face|Scale|Put)[A-Za-z]*|messageShake[A-Za-z]*|(?:bgm|se|cueSe)Stop)(?: +(?:[A-Z]|[-+]?\d+(?:\.\d+)?(?:,[-+]?\d+(?:\.\d+)?)*)){1,10}\]")
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
    with urlopen(Request(url, headers={"User-Agent": "FgoStoryListener/2.11.2-stage3"}), timeout=5) as r:
        data = r.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Script response too large")
    return data.decode("utf-8-sig")

def aligned_pages(raw, slots, normalize, sid, audit=None):
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts = [v["text"].replace("\n", "") for v in slots]
    combined = "".join(parts)
    bounds = {0}
    pos = 0
    for p in parts:
        pos += len(p)
        bounds.add(pos)
    candidates = []
    pages = []
    log_only = set()
    for m in PAGE.finditer(raw):
        name = nameplate(m[1])
        literal = SHAKE.sub("", FX.sub("", m[2]))
        font_only = "[f large]" in literal
        literal = literal.replace("[f large]", "")
        body = normalize(PRESENTATION.sub("", literal))
        if audit is not None:
            # For diagnostics only. Never use stripped unknown directives to
            # generate a translation or activate a cache hit.
            probe = normalize(re.sub(r"\[[^\[\]]+\]", "", body))
            if probe:
                why = ("unknown_name" if name is None else
                       "dynamic" if VARIANT.search(body) else
                       "markup" if "[" in body or "]" in body else "not_unique_or_unaligned")
                audit.setdefault(probe,why)
            for v in (1,2):
                if VARIANT.search(body): audit[VARIANT.sub(lambda m:m[v],body)]="dynamic"
        if name is None or not body or len(body) > 2400:
            continue
        speaker, strict = name
        if strict or font_only or SHAKE.search(m[2]) or PRESENTATION.search(literal):
            log_only.add(m.start())
        skeleton = VARIANT.sub("", body)
        if any(c in skeleton for c in "[]＠＄") or any(c in body for c in "{}"):
            continue
        if not re.fullmatch(r"[^\[\]]*(?:\[&[^:\[\]]+:[^:\[\]]+\][^\[\]]*)*", body):
            continue
        needle = skeleton.replace("\n", "")
        if not needle: continue
        pages.append((m,speaker,body,needle))
        i = combined.find(needle)
        if i < 0 or i not in bounds or i+len(needle) not in bounds or combined.find(needle, i+1) >= 0:
            continue
        candidates.append((i, i+len(needle), m, speaker, body, needle))
    counts = Counter(row[-1] for row in pages)
    named_counts = Counter((row[1],row[-1]) for row in pages)
    candidates = [r for r in candidates if counts[r[-1]] == 1]
    if any(a[1] > b[0] for a,b in zip(candidates,candidates[1:])):
        return []
    # Several distinct ordered anchors identify this script. Its unambiguous
    # named static pages are useful as candidates even outside the anchors;
    # never present one until the game's own LOG confirms the full name/text.
    anchors = {m.start():(start,end) for start,end,m,sp,body,needle in candidates}
    strong = [m.start() for _,_,m,sp,body,needle in candidates
              if len(needle)>=8 and not VARIANT.search(body)]
    out, history = [], []
    prev = prev_slot_end = None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    for m,speaker,body,needle in pages:
        direct = m.start() in anchors
        warm = (not direct and len(strong)>=3 and named_counts[speaker,needle]==1
                and not VARIANT.search(body))
        if not direct and not warm:
            history, prev, prev_slot_end = [], None, None
            continue
        if (prev is None or BREAK.search(raw[prev:m.start()]) or
                (direct and prev_slot_end is not None and anchors[m.start()][0] != prev_slot_end)):
            history = []
        dynamic = bool(VARIANT.search(body))
        if not dynamic:
            out.append(dict(speaker=speaker, text=body,
                            stage_key=f"{sid}:{digest}:{m.start()}" + (":log" if warm or m.start() in log_only else ""),
                            prior=history[-2:]))
        history = (history + [dict(speaker=speaker, ja=VARIANT.sub(r"（候选：\1／\2）",body))])[-2:]
        prev = m.end()
        prev_slot_end = anchors[m.start()][1] if direct else None
    return out

def resolve(quest, slots, normalize, fetch=_get, *, diagnostics=None):
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
        detail={} if diagnostics is not None else None
        plan=aligned_pages(raw,slots,normalize,sid,detail)
        if len(plan) >= 2:
            matches.append((plan,detail))
    if len(matches)!=1: return []
    plan,detail=matches[0]
    if diagnostics is not None: diagnostics.update(detail)
    return plan
