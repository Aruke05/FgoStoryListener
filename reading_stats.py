"""Session-local reading diagnostics. Never send traces to the network."""
from collections import Counter, deque
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid


def wall_ms():
    return time.time_ns() // 1_000_000

def page_key(speaker,text):
    return hashlib.sha256((speaker+"\0"+text).encode()).hexdigest()


class TraceFile:
    """Bounded nonblocking writes. Failure of diagnostics cannot stall gameplay."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.jobs = queue.Queue(maxsize=512)
        self.stop = threading.Event()
        self.dropped = 0
        self.session = uuid.uuid4().hex
        self.thread = threading.Thread(target=self._run, name="ReadingDiagnostics", daemon=True)
        self.thread.start()

    def write(self, event):
        if event.get("event")=="plan_input" and os.environ.get("FGO_READING_REPLAY")=="0":
            return
        try:
            self.jobs.put_nowait(dict(session=self.session, **event))
        except queue.Full:
            self.dropped += 1

    def _run(self):
        while not self.stop.is_set() or not self.jobs.empty():
            try:
                event = self.jobs.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                path=self.directory/"reading.jsonl"
                line=json.dumps(event,ensure_ascii=False,separators=(",",":"))
                if path.exists() and path.stat().st_size + len(line.encode()) > 4_000_000:
                    os.replace(path,self.directory/"reading.previous.jsonl")
                with path.open("a",encoding="utf-8") as fp:
                    fp.write(line+"\n")
            except (OSError, ValueError, TypeError):
                self.dropped += 1

    def close(self):
        self.stop.set()
        self.thread.join(1)


class ReadingStats:
    def __init__(self, log, clock=wall_ms):
        self.log, self.clock = log, clock
        self.rows = deque(maxlen=512)
        self.last = None
        self.plan("", "no_plan")

    def plan(self, quest, state, pages=(), rejected=None, token=None):
        self.quest, self.state, self.plan_ms = quest,state,self.clock()
        self.token = token
        self.candidates = {}
        for p in pages:
            self.candidates.setdefault(p["text"],set()).add(p["speaker"])
        self.rejected = rejected or {}
        signature=pages[0]["stage_key"].split(":")[:2] if pages else []
        self.log.write(dict(event="plan",at=self.plan_ms,quest=quest,state=state,pages=len(pages),
                            token=token,signature=signature))

    def observe(self, item, captured, enabled=True):
        row=item["id"]
        if self.last is not None and self.last['row']==row:
            return
        now=self.clock()
        ready_ms=None
        stamp=item.get("translation_updated_at","")
        if item.get("translation") and stamp:
            try:
                ready_ms=round(datetime.fromisoformat(stamp).timestamp()*1000)
                # Historical timestamps only had second precision. Do not
                # classify a potentially later save as ready at capture time.
                if "." not in stamp: ready_ms+=999
            except (ValueError, OSError, OverflowError):
                pass
        exact=item["speaker"] in self.candidates.get(item["text"],()) and item["quest"]==self.quest
        if exact and item.get("stage_key") and ready_ms is not None:
            ready_ms=max(ready_ms,self.plan_ms)
        ready=None if ready_ms is None and item.get("translation") else bool(ready_ms is not None and ready_ms<=captured)
        if not enabled: why="disabled"
        elif ready: why="ready"
        elif item.get("translation"): why="later_or_unknown"
        elif item["quest"]!=self.quest or self.state!="active": why=self.state if item["quest"]==self.quest else "no_plan"
        elif exact or item.get("stage_key"): why="candidate_pending"
        elif item["text"] in self.candidates: why="speaker_mismatch"
        else: why=self.rejected.get(item["text"],"no_exact_page")
        rec=dict(row=row,quest=item["quest"],captured=captured,seen=now,ready=ready,
                 why=why,first=None,dispatch_ms=max(0,now-captured))
        self.rows.append(rec)
        self.last=rec
        self.last_text=item["text"]
        self.log.write(dict(event="log",key=page_key(item["speaker"],item["text"]),
                            token=self.token,saved_ms=ready_ms,**rec))

    def presented(self,row):
        r=self.last
        if not r or r["row"]!=row or r["first"] is not None: return
        r["first"]=max(0,self.clock()-r["captured"])
        self.log.write(dict(event="first_overlay_idle",row=row,captured=r["captured"],ms=r["first"]))

    def summary(self):
        if not self.last: return "还没有收到游戏 LOG 翻页记录。"
        quest=self.last["quest"]
        rows=[r for r in self.rows if r["quest"]==quest and r["why"]!="disabled"]
        measured=sorted(r["first"] for r in rows if r["first"] is not None)
        labels={"ready":"已就绪","later_or_unknown":"稍后就绪或旧时间精度不足","candidate_pending":"候选尚未译完",
                "lookup":"正在取剧本","offline":"剧本获取失败","unverified":"剧本未核实","no_plan":"无活动预翻计划",
                "speaker_mismatch":"姓名未匹配","no_exact_page":"无对应整页","unknown_name":"姓名未确定",
                "dynamic":"动态台词","markup":"疑似未支持的指令","not_unique_or_unaligned":"重复或顺序未确认"}
        reasons=Counter(r["why"] for r in rows if r["why"]!="ready")
        details="、".join(f"{labels.get(k,k)} {v}" for k,v in reasons.most_common()) or "无"
        p90=f"{measured[math.ceil(.9*len(measured))-1]/1000:.2f} 秒" if measured else "尚无样本"
        slow_ui=sum(r["ready"] is True and r["first"] is not None and r["first"]>=250 for r in rows)
        return (f"{quest}\n游戏 LOG 到达时已有译文：{sum(r['ready'] is True for r in rows)}/{len(rows)} 页"
                f"\n译文窗首次更新 P90：{p90}（{len(measured)} 页有显示记录）\n未就绪原因：{details}"
                f"\n已就绪但窗口更新 ≥0.25 秒：{slow_ui} 页；无显示计时：{len(rows)-len(measured)} 页。"
                "\n从游戏生成 LOG 事件开始计时，不含点击到 LOG 的时间。仅统计本次会话最近 512 条。")
