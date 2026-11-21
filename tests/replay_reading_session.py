"""Recheck recorded sessions without passing played LOG into the planner.

Default is offline: reports actual outcomes/timing. --replan downloads only the
public script for the recorded stage ID; compares coverage at the SAME revision,
not a hypothetical new-model readiness time. The game, AI and history DB are not used.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reading_stats import page_key
import stage_plan
from fgo_story_listener import normalize_text

def report(directory,replan=False):
    events=[]
    for name in ("reading.previous.jsonl","reading.jsonl"):
        path=Path(directory)/name
        if path.is_file():
            with path.open(encoding="utf-8") as inp:
                for line in inp:
                    try: events.append(json.loads(line))
                    except ValueError: continue
    logs=[e for e in events if e["event"]=="log" and e["why"]!="disabled"]
    if not logs: print("还没有可用的翻页记录。"); return
    session=logs[-1]["session"]; quest=logs[-1]["quest"]
    logs=[e for e in logs if e["session"]==session and e["quest"]==quest]
    first={(e["row"],e["captured"]):e["ms"] for e in events if e["session"]==session and e["event"]=="first_overlay_idle"}
    waits=sorted(first[(e["row"],e["captured"])] for e in logs if (e["row"],e["captured"]) in first)
    print(quest,"实际 LOG 就绪",sum(e["ready"] is True for e in logs),"/",len(logs),
          "译文窗记录",len(waits),"P90(ms)",waits[math.ceil(.9*len(waits))-1] if waits else "—")
    print(dict(Counter(e["why"] for e in logs)))
    if not replan: return
    for inp in events:
        if inp["event"]!="plan_input" or inp["session"]!=session or inp["quest"]!=quest: continue
        token=inp["token"]
        ready=next((e for e in events if e["session"]==session and e["event"]=="plan" and e.get("token")==token and e.get("signature")),None)
        if not ready: continue
        # The only evidence the planner receives is the stored pre-play slot stream.
        pages=stage_plan.resolve(inp["ids"],[dict(text=t) for t in inp["slots"]],normalize_text)
        if not pages or pages[0]["stage_key"].split(":")[:2]!=ready["signature"]:
            print("剧本缺失或版本已变，跳过比较。");continue
        keys={page_key(p["speaker"],p["text"]) for p in pages}
        held=[l for l in logs if l.get("token")==token]
        print("同版本剧本候选覆盖",sum(l["key"] in keys for l in held),"/",len(held),"（不代表翻页时已完成翻译）")
if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--replan",action="store_true")
    parser.add_argument("--directory",default=str(Path(os.environ.get("LOCALAPPDATA","."))/"FgoStoryListener"/"reading-diagnostics"))
    opt=parser.parse_args();report(opt.directory,opt.replan)
