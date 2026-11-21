"""Manual read-only coverage benchmark for the stage1 phase-2 delay report.
Rebuild a conservative slot input from only the old verified anchors. Played
LOG is held out for replay. No model calls: measures ready-cache coverage,
not network/first-token latency. Usage: python tests/replay_stage_speed.py
"""
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fgo_story_listener as app
import stage_plan

def run():
    dbpath = Path(os.environ["LOCALAPPDATA"])/"FgoStoryListener/history.db"
    db = sqlite3.connect(dbpath.as_uri()+"?mode=ro", uri=True)
    try:
        db.execute("BEGIN")
        rows = db.execute("SELECT stage_key,text,quest FROM dialogue WHERE stage_key LIKE '9416400720:%' AND id<=29692 ORDER BY id").fetchall()
        if not rows:
            raise SystemExit("No matching historical stage1 recording.")
        label = rows[0][2]
        played = db.execute("SELECT speaker,text,stage_key FROM reading_dialogue WHERE quest=? "
                            "AND source='ScriptBackLog.logData' AND speaker<>'【已选择】' "
                            "AND captured_at<='2026-11-21T19:33:43.204' ORDER BY captured_at,id",(label,)).fetchall()
    finally:
        db.close()
    slots = [dict(text=text) for key,text,_ in sorted(rows,key=lambda row:int(row[0].split(':')[2]))]
    pages = stage_plan.resolve(dict(quest_id=94164007,phase=2), slots, app.normalize_text)
    assert pages and pages[0]["stage_key"].split(":")[1]==rows[0][0].split(":")[1], "Script revision changed"
    with tempfile.TemporaryDirectory() as d:
        store = app.HistoryStore(Path(d)/"replay.db")
        try:
            now = datetime.now()
            ids,_ = store.add_preloaded(pages,label,now,stage_plan.SOURCE)
            store.save_translations([dict(id=i,zh=f"cached-{i}") for i in ids],"fixture")
            log, durations, totals = [], [], Counter()
            for speaker,text,old_key in played:
                log.append(dict(speaker=speaker,text=text))
                start = time.perf_counter()
                store.add_backlog(log,label,now,1000)
                visible = store.latest_visible_id(label,speaker,text)
                hit = bool(store.translation_items([visible])[0]["translation"])
                if hit: durations.append((time.perf_counter()-start)*1000)
                totals.update(pages=1,old=int(bool(old_key)),new=int(hit),multiline=int("\n" in text),
                              multiline_hit=int(hit and "\n" in text))
            print(dict(totals),"planned",len(pages),"median_ready_cache_ms",round(statistics.median(durations),2))
        finally:
            store.close()

if __name__=="__main__":
    run()
