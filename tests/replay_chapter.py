"""Replay historical LOG against a fresh plan; --online measures actual preload completion times.
Preparation inputs are only public script + surviving exact anchors (not a raw game event recording).
No historical translations/played text available to the planning or translation worker.
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fgo_story_listener as f
import stage_plan as sp

def run(sid,online):
    src=sqlite3.connect((Path(os.environ["LOCALAPPDATA"])/"FgoStoryListener/history.db").as_uri()+"?mode=ro",uri=True)
    src.execute("BEGIN")
    anchors=src.execute("select distinct stage_key,quest from dialogue where stage_key like ? and stage_key not like '%:log'",(sid+":%",)).fetchall()
    if not anchors: raise ValueError("No historical anchors for "+sid)
    q=anchors[0][1]
    origin=src.execute("select min(captured_at) from dialogue where preloaded=1 and stage_key like ?",(sid+":%",)).fetchone()[0]
    if not origin: raise ValueError("Historical plan timestamp unavailable")
    t0=time.monotonic()
    raw=sp._get(f"https://static.atlasacademy.io/JP/Script/{sid[:2]}/{sid[:4]}/{sid}.txt").replace("\r\n","\n").replace("\r","\n")
    from hashlib import sha256
    digest=sha256(raw.encode()).hexdigest()
    offsets={int(key.split(':')[2]) for key,_ in anchors if key.split(':')[1]==digest}
    if len(offsets)<3: raise ValueError("Script revision does not match the recording")
    slots=[dict(text=f.normalize_text(sp.FX.sub("",m[2]))) for m in sp.PAGE.finditer(raw) if m.start() in offsets]
    plan=sp.resolve(dict(quest_id=int(sid[:-2]),phase=int(sid[-2]),script=sid),slots,f.normalize_text)
    assert plan
    cfg=f.RuntimeConfig.load()
    with tempfile.TemporaryDirectory() as d,patch.object(f,'data_dir',return_value=Path(d)):
        db=f.HistoryStore(Path(d)/"translation.db")
        try:
            ids,_=db.add_preloaded(plan,q,datetime.now(),sp.SOURCE)
            times={}
            values={}
            if online:
                events=queue.Queue()
                worker=f.CodexTranslationWorker(db,cfg,events,"preload")
                worker.enqueue(ids); worker.start()
                try:
                    deadline=time.monotonic()+600
                    while len(times)<len(ids):
                        event=events.get(timeout=max(0.1,deadline-time.monotonic()))
                        if event["type"]=="translation_error" or event.get("failed_ids"):
                            raise RuntimeError(str(event.get("message") or event.get("failed_ids")))
                        if event["type"] not in ("translation_partial","translation_complete"): continue
                        for item in event.get("items",[]):
                            rid=item['id']
                            times.setdefault(rid,time.monotonic()-t0)
                            values[rid]=item['translation']
                        if event["type"]=="translation_complete": print("translated",len(times),"/",len(ids),flush=True)
                finally:
                    worker.close();worker.join(10)
            # Open held-out playback sequence only after the new plan is frozen.
            until="2026-11-21T20:38:14.268" if sid=="9416400810" else "2026-11-21T19:47:30.999"
            rows=src.execute("select speaker,text,captured_at from reading_dialogue where quest=? and source='ScriptBackLog.logData' and captured_at between ? and ? order by captured_at,id",(q,origin,until)).fetchall()
            stats=dict(played=0,eligible=0,ready=0); base=datetime.fromisoformat(origin)
            keys={(sp,ja):rid for rid,sp,ja in db._db.execute("select id,speaker,text from dialogue")}
            prefixes=[]
            replay=f.HistoryStore(Path(d)/"play.db")
            try:
                reids,_=replay.add_preloaded(plan,q,base,sp.SOURCE)
                mapping=dict(zip(ids,reids)); emitted=set()
                for speaker,text,stamp in rows:
                    t=datetime.fromisoformat(stamp); seconds=(t-base).total_seconds()
                    due=[rid for rid in ids if rid not in emitted and (not online or times[rid]<=seconds)]
                    if due:
                        replay.save_translations([dict(id=mapping[i],zh=values.get(i,'fixture')) for i in due],"replay")
                        emitted.update(due)
                    prefixes.append(dict(speaker=speaker,text=text))
                    tail=[]
                    replay.add_backlog(prefixes,q,t,1000,tail_ids=tail)
                    if speaker.startswith("【"): continue
                    stats["played"]+=1; rid=keys.get((speaker,text))
                    stats["eligible"]+=rid is not None or f.literal_pause(text)
                    visible=tail[-1]
                    ready=bool(visible and replay.translation_items([visible])[0]["translation"])
                    stats["ready"]+=ready
                    if text=="……如何致しましたかな。":
                        print("target",speaker,"log_offset_s",round(seconds,2),"ready_offset_s",round(times.get(rid,0),2) if online else "fixture","hit",ready,values.get(rid,""))
                print(sid,"plan",len(plan),stats,"timed online" if online else "coverage only")
            finally: replay.close()
        finally: db.close();src.close()
if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("sid",choices=["9416400720","9416400810"])
    ap.add_argument("--online",action="store_true")
    a=ap.parse_args()
    run(a.sid,a.online)
