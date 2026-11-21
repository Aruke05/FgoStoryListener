from datetime import datetime
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

import fgo_story_listener as f
import stage_plan as sp
from reading_stats import ReadingStats, TraceFile

def page(who, what): return f"＠{who}\n{what}\n[k]\n"
anchor=["昨日からここで待っていた。","一緒に行きましょう。","そろそろ始まりそうだ。"]
def plan(extra):
    src="".join(page("マシュ",t) for t in anchor)+extra
    return sp.aligned_pages(src,[dict(text=t) for t in anchor],f.normalize_text,"id")

class ReaderExperience(TestCase):
    def test_background_burst_yields_without_losing_events(self):
        ui=Mock()
        ui.events=queue.Queue()
        for i in range(99): ui.events.put(i)
        handled=[]
        ui._handle_event.side_effect=handled.append
        f.Application._poll_events(ui)
        self.assertLessEqual(len(handled),32)
        self.assertGreater(len(handled),0)
        while not ui.events.empty(): f.Application._poll_events(ui)
        self.assertEqual(handled,list(range(99)))
    def test_same_body_different_explicit_names_and_unknown_slots(self):
        text="……如何致しましたかな。"
        pp=plan(page("A：松永久秀",text)+page("Ｉ：三好長慶",text)+page("A","不明な台詞。"))
        self.assertEqual([(p['speaker'],p['text']) for p in pp[3:]],[("松永久秀",text),("三好長慶",text)])
        self.assertTrue(all(p['stage_key'].endswith(':log') for p in pp[3:]))
        self.assertEqual(len(plan(page("A：松永久秀",text)*2)),3)
        self.assertEqual(len(plan(page("A：[npc]",text))),3)
    def test_warm_before_and_after_anchor_but_no_effect_on_weak_plan(self):
        src=page("I：松永久秀","最初の一言。")+"".join(page("マシュ",x) for x in anchor)+page("A：松永久秀","最後の一言。")
        for n,count in [(2,2),(3,5)]:
            self.assertEqual(len(sp.aligned_pages(src,[dict(text=x) for x in anchor[:n]],f.normalize_text,"s")),count)
    def test_markup_no_loss_or_guess(self):
        pp=plan(page("I：松永弾正","[f large]それは[r][messageShake 0.03 3 3 0.5]私だ。")
                +page("マシュ","私は[unresolved choice]です。"))
        self.assertEqual(pp[-1]["text"],"それは\n私だ。")
        self.assertTrue(pp[-1]["stage_key"].endswith(":log"))
    def test_cache_survives_sequence_reordering_and_exact_tail(self):
        with TemporaryDirectory() as d:
            db=f.HistoryStore(Path(d)/"h.db")
            try:
                now=datetime.now()
                prefix=[dict(speaker="マシュ",text=f"前文{i}。") for i in range(15)]
                db.add_backlog(prefix,"q",now,0)
                last=dict(speaker="松永久秀",text="……如何致しましたかな。",stage_key="s:v:35:log")
                ids,_=db.add_preloaded([last],"q",now,sp.SOURCE)
                db.save_translations([dict(id=ids[0],zh="……您怎么了？")],"model")
                tail=[]
                pending=db.add_backlog(prefix+[last],"q",now,0,tail_ids=tail)
                self.assertFalse(pending)
                self.assertEqual(tail,ids)
                self.assertEqual(db.rows()[-1][6],"……您怎么了？")
                self.assertEqual(len(db.rows()),16)
                self.assertEqual(db.add_backlog(prefix+[last],"q",now,0),[])
            finally: db.close()
    def test_repeat_silence_requires_no_ai(self):
        with TemporaryDirectory() as d:
            db=f.HistoryStore(Path(d)/"h.db")
            try:
                t="…………。"; now=datetime.now()
                db.add_backlog([dict(speaker="三好",text=t)]*2,"q",now,0)
                self.assertFalse(db.untranslated_ids())
                self.assertEqual([r[6] for r in db.rows()],[t,t])
                self.assertFalse(f.literal_pause("……いや。"))
            finally: db.close()
    def test_restart_preserves_verified_multiline_translation(self):
        with TemporaryDirectory() as d:
            path=Path(d)/"h.db"; db=f.HistoryStore(path)
            try:
                t=datetime(2026,11,21,19,50)
                p=dict(speaker="松永久秀",text="一行。\n二行。",stage_key="s:v:1:log")
                ids,_=db.add_preloaded([p],"q",t,sp.SOURCE)
                db.save_translations([dict(id=ids[0],zh="一句。\n二句。")],"model")
                db.add_backlog([p],"q",datetime(2099,1,1),1)
            finally: db.close()
            db=f.HistoryStore(path)
            try: self.assertEqual(db.rows()[0][6],"一句。\n二句。")
            finally: db.close()
    def test_preview_never_flashes_old_translation_ahead_of_log_candidate(self):
        with TemporaryDirectory() as d:
            db=f.HistoryStore(Path(d)/"h.db")
            try:
                now=datetime.now()
                entry=dict(speaker="松永久秀",text="祝着至極にございます。",stage_key="s:v:8:log")
                old=db.add(entry["speaker"],entry["text"],"live",now,"q")
                db.save_translations([dict(id=old,zh="旧译")],"old")
                ids,_=db.add_preloaded([entry],"q",now,sp.SOURCE)
                db.save_translations([dict(id=ids[0],zh="幸甚之至。")],"new")
                self.assertFalse(db.cached_page_translation("q",entry["speaker"],entry["text"]))
                tail=[]
                db.add_backlog([entry],"q",now,0,tail_ids=tail)
                self.assertEqual(db.translation_items(tail)[0]["translation"],"幸甚之至。")
            finally: db.close()
    def test_sample_real_clock_separates_hit_pending_and_unseen(self):
        log=Mock(); now=[1800000100000]
        stats=ReadingStats(log,lambda:now[0])
        stats.plan("q","active",[dict(text="頁",speaker="人",stage_key="a:b:1")])
        def row(i,stamp=0):
            return dict(id=i,speaker="人",text="頁",quest="q",stage_key="a:b:1",
                        translation="好" if stamp else "",
                        translation_updated_at=datetime.fromtimestamp(stamp/1000).isoformat(timespec='milliseconds') if stamp else "")
        stats.observe(row(1,1800000100000),1800000100001);now[0]=1800000100019;stats.presented(1)
        stats.observe(row(1,1800000100000),1800000100001)
        stats.observe(row(2),1800000100020)
        now[0]=1800000101020;stats.presented(2);stats.presented(2)
        stats.observe(row(3),1800000101021)
        self.assertEqual([r["first"] for r in stats.rows],[18,1000,None])
        self.assertIn("1/3",stats.summary()); self.assertIn("1.00 秒",stats.summary())
        self.assertEqual(stats.last["why"],"candidate_pending")
