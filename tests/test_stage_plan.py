"""Himiko regression: fixture reconstructed from three actual LOG pages and five runtime slots."""
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
import queue
import threading
from unittest.mock import Mock, patch
import fgo_story_listener as f
import stage_plan as sp

H,M,N="卑弥呼","松永弾正","水着ノッブ"
A="壱与ちゃんはあたしがなんとかするから、"
B="ノッブちゃんと弾正ちゃんは"
C="をお願い！"
D="ノッブちゃんて、何そのゆるキャラみたいなあだ名。"
E="弾正ちゃんとか言ってる奴に言われたくないんじゃけど。"
FULL=A+"\n"+B+"未来クン"+C
SCRIPT=f"＠{H}\n{A}[r]{B}[&未来クン:未来チャン]{C}\n[k]\n＠{M}\n{D}\n[k]\n＠{N}\n{E}\n[k]\n"
SLOTS=[dict(speaker=N,text=t) for t in (A,B,C,D,E)]
Q={"quest_id":94164007,"phase":1}
def pages(raw=SCRIPT, slots=SLOTS):
    return sp.aligned_pages(raw,slots,f.normalize_text,"9416400710")

class StagePlanTests(unittest.TestCase):
    def test_inbound_event_to_preload_queue_and_late_result(self):
        events=queue.Queue()
        proc=f.DialogueProcessor(Mock(),events,threading.Event())
        with tempfile.TemporaryDirectory() as temp:
            db=f.HistoryStore(Path(temp)/"test.db")
            ui=Mock(store=db,events=events)
            ui._stage_token=None
            ui.show_preloaded_rows.get.return_value=False
            try:
                proc.notify(dict(type="scenario_plan",source="ScriptManager.VisibleTextSlots",
                                 quest=Q,entries=[dict(s,kind="dialogue_fragment") for s in SLOTS]))
                lookup=events.get(timeout=2)
                self.assertEqual(lookup["type"],"scenario_lookup")
                with patch.object(sp,"resolve",side_effect=lambda q,s,n:sp.aligned_pages(SCRIPT,s,n,"id")):
                    f.Application._handle_event(ui,lookup)
                    ready=events.get(timeout=2)
                f.Application._handle_event(ui,ready)
                queued=ui.preload_translator.enqueue.call_args.args[0]
                batch=db.translation_items(queued)
                self.assertEqual([(i["speaker"],i["text"]) for i in batch],[(M,D),(N,E)])
                self.assertEqual(batch[-1]["stage_prior"][0]["speaker"],H)
                ui._stage_token=None
                f.Application._handle_event(ui,ready)
                ui.preload_translator.enqueue.assert_called_once()
            finally:
                proc.close()
                db.close()
    def test_actual_shape_and_fallback(self):
        entries=pages()
        self.assertEqual([p["speaker"] for p in entries],[M,N])
        self.assertEqual([p["speaker"] for p in entries[-1]["prior"]],[H,M])
        self.assertIn("未来クン／未来チャン",entries[-1]["prior"][0]["ja"])
        with tempfile.TemporaryDirectory() as temp:
            db=f.HistoryStore(Path(temp)/"test.db")
            try:
                now=datetime.now()
                old=db.add("水着ノッブ",E,"live",now,"q")
                db.save_translations([dict(id=old,zh="管别人叫小弹正的家伙")],"old")
                ids,n=db.add_preloaded(entries,"q",now,sp.SOURCE)
                self.assertEqual((len(ids),n),(2,2))
                self.assertIsNone(db.activate_preloaded("別人",E,"live",now,"q","","",1))
                self.assertEqual(db.translation_items(ids)[1]["stage_prior"],entries[1]["prior"])
                db.save_translations([dict(id=i,zh=z) for i,z in zip(ids,("甲","乙"))],"test")
                pending=db.add_backlog([dict(speaker=H,text=FULL),*entries],"q",now,1000)
                self.assertTrue(any(r["text"]==FULL for r in db.translation_items(pending)))
                self.assertEqual(db.translation_items([old])[0]["translation"],"乙")
            finally: db.close()
    def test_suspended_stage_cannot_activate_or_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            db=f.HistoryStore(Path(temp)/"test.db")
            try:
                ids,_=db.add_preloaded(pages(),"q",datetime.now(),sp.SOURCE)
                db.suspend_verified_plan()
                self.assertFalse(db.translation_items(ids))
                self.assertFalse(db.preloaded_untranslated_ids("q"))
                self.assertIsNone(db.activate_preloaded(M,D,"live",datetime.now(),"q",M,D,1))
            finally: db.close()
    def test_wrong_played_variant_or_speaker_does_not_repair_history(self):
        prior=pages()[-1]["prior"]
        self.assertTrue(sp.matches_prior(prior,[(H,FULL),(M,D)]))
        self.assertFalse(sp.matches_prior(prior,[(N,FULL),(M,D)]))
        self.assertFalse(sp.matches_prior(prior,[(H,FULL.replace("未来クン","別人")),(M,D)]))
    def test_reversed_ambiguous_and_missing(self):
        self.assertFalse(pages(slots=list(reversed(SLOTS))))
        self.assertNotIn(E,[p["text"] for p in pages(slots=SLOTS+[SLOTS[-1]])])
        self.assertNotIn(D,[p["text"] for p in pages(slots=SLOTS[:-2]+SLOTS[-1:])])
    def test_unknown_branch_does_not_supply_prior(self):
        result=pages(SCRIPT.replace("＠"+N,"[label alternate]\n＠"+N))
        self.assertEqual(result[-1]["prior"],[])
    def test_multiple_matching_scripts_or_wrong_id_fail_closed(self):
        def fetch(url):
            if "api.atlas" in url: return json.dumps(dict(id=94164007,phase=1,scripts=[dict(scriptId=i) for i in ("9416400710","9416400711")]))
            return SCRIPT
        self.assertEqual(sp.resolve(Q,SLOTS,f.normalize_text,fetch),[])
        self.assertEqual(sp.resolve(dict(Q,script="9999999999"),SLOTS,f.normalize_text,fetch),[])
        self.assertEqual(len(sp.resolve(dict(Q,script="9416400710"),SLOTS,f.normalize_text,fetch)),2)
