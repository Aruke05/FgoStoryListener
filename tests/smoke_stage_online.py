"""Manual online smoke: use real JP script, production translator and an isolated database."""
import os
import sys
import tempfile
import queue
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fgo_story_listener as app
import stage_plan

def run():
    config = app.RuntimeConfig.load()
    from test_stage_plan import SLOTS, H, M, N, FULL
    with tempfile.TemporaryDirectory() as d, patch.object(app, 'data_dir', return_value=Path(d)):
        store = app.HistoryStore(Path(d)/"test.db")
        now = datetime.now()
        q = "第７話 禁門チキンレース（阶段 1）"
        pages = stage_plan.resolve({"quest_id":94164007,"phase":1}, SLOTS, app.normalize_text)
        assert [p["speaker"] for p in pages] == [M,N]
        ids,_ = store.add_preloaded(pages,q,now,stage_plan.SOURCE)
        def translate(ids,scope):
            worker=app.CodexTranslationWorker(store,config,queue.Queue(),scope)
            worker.enqueue(ids)
            batch=worker._take_batch()
            assert [x["id"] for x in batch] == ids
            try:
                prompt=worker._build_prompt(batch)
                result=worker._invoke(q,prompt,is_preload=scope=="preload")
                saved=store.save_translations(result["translations"],worker._profile(scope=="preload")[0])
                assert {i["id"] for i in saved}==set(ids)
                return result
            finally:
                worker._app_server.close()
        try:
            translate(ids,"preload")
            backlog=[dict(speaker=H,text=FULL), *pages]
            live=store.add_backlog(backlog,q,now,1000)
            assert len(live)==1 and live[0] not in ids
            translate(live,"live")
            rows=list(store._db.execute("select speaker,text,translation from dialogue where preloaded=0 order by display_order,id"))
            assert [r[0] for r in rows]==[H,M,N] and all(r[2] for r in rows)
            for r in rows: print(r[0],r[2])
        finally: store.close()
if __name__=="__main__": run()
