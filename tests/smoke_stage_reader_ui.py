"""Smoke the actual Tk reader; transparent test windows, temp DB, no emulator or model."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fgo_story_listener as f
import stage_plan as sp

def run():
    with tempfile.TemporaryDirectory() as d,patch.object(f,'data_dir',return_value=Path(d)),patch.object(f.Application,'start_listener'),patch.object(f.Application,'_start_translator'):
        ui=f.Application()
        try:
            ui.attributes("-alpha",0)
            for window in (ui.overlay,ui.log_window,ui.explanation_window):
                window.attributes("-alpha",0)
            ui.update()
            now=datetime.now()
            ja="……如何致しましたかな。"; zh="……您这是怎么了？"; quest="回放"
            entries=[dict(speaker="松永久秀",text=ja,stage_key="script:hash:32:log")]
            ids,_=ui.store.add_preloaded(entries,quest,now,sp.SOURCE)
            ui.store.save_translations([dict(id=ids[0],zh=zh)],"fixture")
            ui.reading.plan(quest,"active",entries)
            ui.overlay.show_preview(quest,"松永久秀",ja)
            assert ui.overlay.translation_var.get()=="已预译，等待游戏确认…"
            assert not ui.tree.exists(str(ids[0]))
            ui._handle_event(dict(type="backlog",quest=quest,source="ScriptBackLog.logData",
                                  captured_at_ms=int(datetime.now().timestamp()*1000),entries=entries))
            ui.update()
            assert ui.current_row_id==ids[0]
            assert ui.overlay.translation_var.get()==zh
            assert ui.reading.last["first"] is not None, ui.reading.last
            ui._handle_event(dict(type="backlog",quest=quest,entries=entries))
            assert len(ui.reading.rows)==1
            print(ui.reading.summary())
        finally: ui.on_close()
        path=Path(d)/"reading-diagnostics/reading.jsonl"
        rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        assert sum(r["event"]=="log" for r in rows)==1 and sum(r["event"]=="first_overlay_idle" for r in rows)==1
        print("Tk overlay, deduplication and persisted first-text event verified.")
if __name__=="__main__":run()
