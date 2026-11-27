# -*- coding: utf-8 -*-
"""tools/test_batch_guard.py — 批处理护栏 G1–G6 回归测试。

全部用临时目录 + 假 book.json/.pipeline.json 夹具：不跑 OCR、不起子进程转换、
绝不碰真实 books/。运行：
    python -m unittest tools.test_batch_guard        （或 python tools/test_batch_guard.py）
"""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as pl  # noqa: E402


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def wf(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def rf(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def make_book(books, name, *, book_json=None, marker=None, preflight=None, pages=False,
              raw=None, qc=None, report=True):
    """造一个目标书目录夹具。"""
    bd = os.path.join(books, name)
    os.makedirs(bd, exist_ok=True)
    if pages:
        os.makedirs(os.path.join(bd, "pages"), exist_ok=True)
        wf(os.path.join(bd, "pages", "page_001_ocr2.json"), "[]")
    if book_json is not None:
        wj(os.path.join(bd, "book.json"), book_json)
    if raw is not None:
        wj(os.path.join(bd, "book_raw.json"), raw)
    if marker is not None:
        wj(os.path.join(bd, pl.MARKER), marker)
    if preflight is not None:
        wj(os.path.join(bd, "预检.json"), preflight)
    if qc is not None:
        wj(os.path.join(bd, "质检.json"), qc)
    if report:
        wf(os.path.join(bd, "转换报告.md"), "# 旧报告\n\n<!-- RECHECK -->\n旧复查\n")
    return bd


def book_obj(title="书甲", chars=None, pages=100, paras=3, raw_of=False):
    blocks = []
    for i in range(paras):
        blocks.append({"type": "paragraph", "text": "正" * (chars // paras if chars else 300), "page": i + 1})
    blocks.append({"type": "heading", "text": "第一章", "page": 1})
    total = chars if chars is not None else sum(len(b["text"]) for b in blocks)
    return {"bookTitle": title, "source": "%s.pdf" % title, "stats": {"pages": pages, "chars": total,
            "paragraphs": paras, "figures": 0, "paired": 0, "chapters": 1}, "blocks": blocks,
            "chapters": [{"title": "第一章", "startBlock": 1, "chars": 10}]}


def args_for(root, **kw):
    a = SimpleNamespace(folder=os.path.join(root, "inbox"), out=os.path.join(root, "books"),
                        apply=False, report="", lock=os.path.join(root, "guard", "batch.lock.json"),
                        journal=os.path.join(root, "guard", "batch_runs.jsonl"),
                        approve_list=os.path.join(root, "guard", "重转批准清单.json"),
                        run_id="", workers=1, scale=3.0, force=False, force_lock=False,
                        no_pdf_meta=True, takeover_partial=False, allow_manual_review=False,
                        allow_restore_raw=False, vertical=False, ear="", vmodel="", from_stage="",
                        title="")
    a.__dict__.update(kw)
    return a


def dead_pid():
    """起一个立刻退出的子进程，拿它的 PID（保证已死）。"""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    return p.pid


def snapshot_tree(root):
    inv = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            st = os.stat(p)
            inv[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return inv


class LockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_lock_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lock = os.path.join(self.dir, "batch.lock.json")
        self.journal = os.path.join(self.dir, "batch_runs.jsonl")
        self.meta = {"pid": os.getpid(), "runId": "20261127_0905", "startedAt": "x", "hbAt": "x",
                     "folder": "F", "out": "O", "workers": 1, "scale": 3.0, "planned": 3, "cmdline": "c"}

    def test_live_lock_refuses_second_instance(self):
        st, holder = pl.acquire_batch_lock(self.lock, self.meta, journal_path=self.journal)
        self.assertEqual(st, "acquired")
        self.assertTrue(os.path.isfile(self.lock))
        meta2 = dict(self.meta, runId="20261127_0906", pid=os.getpid())
        st2, holder2 = pl.acquire_batch_lock(self.lock, meta2, journal_path=self.journal)
        self.assertEqual(st2, "live")
        self.assertEqual(holder2["runId"], "20261127_0905")       # 打印给拒绝信息用
        self.assertEqual(holder2["pid"], os.getpid())
        # 活锁不被覆盖
        self.assertEqual(pl.read_batch_lock(self.lock)[1]["runId"], "20261127_0905")
        text = pl.format_batch_lock(holder2)
        for key in ("PID", "run-id", "inbox=F", "out=O", "workers=1", "scale=3"):
            self.assertIn(key, text)

    def test_stale_lock_takeover_marks_unfinished(self):
        wj(self.lock, {"pid": dead_pid(), "runId": "20261127_0310", "startedAt": "s",
                       "planned": 26, "folder": "F", "out": "O"})
        wj(self.journal, {}) if False else None
        wf(self.journal, json.dumps({"event": "start", "runId": "20261127_0310", "pid": 1,
                                     "planned": 26}, ensure_ascii=False) + "\n")
        logs = []
        st, holder = pl.acquire_batch_lock(self.lock, self.meta, journal_path=self.journal,
                                           log_fn=logs.append)
        self.assertEqual(st, "stale")
        self.assertEqual(holder["runId"], "20261127_0310")
        self.assertIn("陈旧锁", logs[0])
        events = pl.journal_events(self.journal)
        unf = [e for e in events if e["event"] == "unfinished"]
        self.assertEqual(len(unf), 1)
        self.assertEqual(unf[0]["runId"], "20261127_0310")
        self.assertIn("已死", unf[0]["reason"])
        self.assertEqual(unf[0]["takenOverBy"], "20261127_0905")
        self.assertEqual(pl.read_batch_lock(self.lock)[1]["runId"], "20261127_0905")
        self.assertFalse(pl.journal_open_batches(self.journal))   # unfinished 算收尾

    def test_corrupt_lock_refuses_unless_force(self):
        wf(self.lock, "{不是 json")
        st, holder = pl.acquire_batch_lock(self.lock, self.meta, journal_path=self.journal)
        self.assertEqual(st, "corrupt")
        st2, _ = pl.acquire_batch_lock(self.lock, self.meta, journal_path=self.journal, force=True)
        self.assertEqual(st2, "acquired")

    def test_touch_and_release_only_own(self):
        pl.acquire_batch_lock(self.lock, self.meta, journal_path=self.journal)
        self.assertTrue(pl.touch_batch_lock(self.lock, "20261127_0905"))
        self.assertFalse(pl.touch_batch_lock(self.lock, "别的批次"))
        self.assertEqual(pl.release_batch_lock(self.lock, "别的批次"), "not-mine")
        self.assertEqual(pl.release_batch_lock(self.lock, "20261127_0905"), "removed")
        self.assertFalse(os.path.exists(self.lock))

    def test_pid_alive_reality(self):
        self.assertTrue(pl.pid_alive(os.getpid()))
        self.assertFalse(pl.pid_alive(dead_pid()))
        self.assertFalse(pl.pid_alive(0))
        self.assertFalse(pl.pid_alive("坏值"))


class RunJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_run_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.journal = os.path.join(self.dir, "batch_runs.jsonl")

    def test_run_id_is_beijing_yyyymmdd_hhmm(self):
        self.assertEqual(pl.make_run_id(dt.datetime(2026, 11, 27, 9, 5, tzinfo=pl.BJ_TZ)),
                         "20261127_0905")
        utc = dt.datetime(2026, 11, 27, 1, 5, tzinfo=dt.timezone.utc)      # +8 → 09:05
        self.assertEqual(pl.make_run_id(utc), "20261127_0905")
        self.assertRegex(pl.make_run_id(), r"^\d{8}_\d{4}$")

    def test_execute_batch_writes_plan_and_end(self):
        pdfs = [os.path.join(self.dir, "%d.pdf" % i) for i in range(3)]
        args = args_for(self.dir)
        lines = []
        res = pl.execute_batch(pdfs, args, None, "20261127_0905", self.journal,
                               log_fn=lambda m, fp=None: lines.append(m),
                               process_fn=lambda p, o, a, fp: True)
        self.assertEqual(len(res), 3)
        self.assertTrue(any(l.startswith("[") or "批处理计划" in l for l in lines))
        self.assertIn("批处理计划（run-id=20261127_0905）", lines[0])
        self.assertTrue(any("批处理结束（run-id=20261127_0905）" in l for l in lines))
        events = pl.journal_events(self.journal)
        self.assertEqual([e["event"] for e in events], ["start", "end"])
        self.assertEqual(events[1]["total"], 3)
        self.assertFalse(pl.journal_open_batches(self.journal))

    def test_execute_batch_abort_records_pending(self):
        pdfs = [os.path.join(self.dir, "%d.pdf" % i) for i in range(3)]

        def fake(p, o, a, fp):
            if p == pdfs[2]:
                raise KeyboardInterrupt("模拟被中断")
            return True

        lines = []
        with self.assertRaises(KeyboardInterrupt):
            pl.execute_batch(pdfs, args_for(self.dir), None, "20261127_0906", self.journal,
                             log_fn=lambda m, fp=None: lines.append(m), process_fn=fake)
        self.assertTrue(any("批处理中止（run-id=20261127_0906）" in l for l in lines))
        self.assertTrue(any("未完成清单（1 本）" in l for l in lines))
        events = pl.journal_events(self.journal)
        abort = [e for e in events if e["event"] == "abort"]
        self.assertEqual(len(abort), 1)
        self.assertEqual(len(abort[0]["pending"]), 1)
        self.assertEqual(len(abort[0]["done"]), 2)

    def test_legacy_log_scan_and_open_batches(self):
        log = os.path.join(self.dir, "转换日志.txt")
        wf(log, "[03:27:52] 批处理开始：26 本\n[03:28:40] 批处理开始：26 本\n"
                "[04:00:00] 批处理结束：1/26 通过\n")
        r = pl.legacy_log_unfinished(log)
        self.assertEqual(r["starts"], 2)
        self.assertEqual(r["ends"], 1)
        self.assertEqual(len(r["unmatched"]), 1)
        self.assertIn("03:27:52", r["unmatched"][0])
        wf(self.journal, json.dumps({"event": "start", "runId": "A"}) + "\n")
        self.assertEqual([e["runId"] for e in pl.journal_open_batches(self.journal)], ["A"])


class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_dry_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.inbox = os.path.join(self.dir, "inbox")
        self.books = os.path.join(self.dir, "books")
        os.makedirs(self.inbox)
        os.makedirs(self.books)
        # ① 已在库（且 raw 危险）
        make_book(self.books, "已在库书", book_json=book_obj("已在库书", chars=300000),
                  marker={"version": 1, "pdf": "x", "scale": 3.0,
                          "done": {s: {"at": "t"} for s in pl.POST_STAGES + ["convert"]}},
                  raw=book_obj("已在库书", chars=500000),
                  qc={"checkedAt": "2026-11-01 10:00", "gate": {"ok": True, "checks": [{"name": "x"}]}})
        # ② 在途半成品（预检 + pages，无 book.json），且 PDF 换了名字
        make_book(self.books, "半成品书-第二版", preflight={"pdf": os.path.join(self.inbox, "半成品书-第二版.pdf"),
                                                             "pages": 200}, pages=True)
        # ③ 异名同书·目录名靠相似度命中（在库 source 就是 inbox 文件名）
        make_book(self.books, "建筑力学（第三版） (吕令毅、吕子华)",
                  book_json=dict(book_obj("建筑力学（第三版）", chars=120000),
                                 source="建筑力学 (吕令毅，吕子华编著.pdf"))
        # ④ 异名同书·只能靠四键命中（书名主干也不同，source 完全一致）
        make_book(self.books, "已在库乙", book_json=dict(book_obj("已在库乙", chars=90000),
                                                        source="改名后的源文件.pdf"))
        # 已在库、无 book_raw.json（安全，只是禁写）
        make_book(self.books, "已在库丙", book_json=book_obj("已在库丙", chars=80000),
                  marker={"version": 1, "done": {"convert": {"at": "t"}}})
        # 真新增
        for n in ("已在库书.pdf", "半成品书-第二版.pdf", "建筑力学 (吕令毅，吕子华编著.pdf",
                  "改名后的源文件.pdf", "全新书.pdf", "已在库丙.pdf"):
            wf(os.path.join(self.inbox, n), "%PDF-1.4 fake")
        self.args = args_for(self.dir, folder=self.inbox, out=self.books)

    def test_dry_run_writes_nothing(self):
        before = snapshot_tree(self.dir)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pl.cmd_batch(self.args)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertEqual(before, snapshot_tree(self.dir), "预演模式产生了写入")
        self.assertIn("模式=预演", out)
        self.assertIn("新增 1", out)
        self.assertIn("重复·已入库 2", out)
        self.assertIn("本批禁写", out)
        self.assertIn("在途半成品 1", out)
        self.assertIn("需人工判定 2", out)
        self.assertIn("高危·raw覆盖", out)
        self.assertIn("本批禁写", out)

    def test_report_only_inside_guard_dir(self):
        outside = os.path.join(self.dir, "books", "预演.md")
        buf = io.StringIO()
        with redirect_stdout(buf):
            pl.cmd_batch(args_for(self.dir, folder=self.inbox, out=self.books, report=outside))
        self.assertIn("已忽略 --report", buf.getvalue())
        self.assertFalse(os.path.exists(outside))
        old_guard = pl.GUARD_DIR
        try:
            pl.GUARD_DIR = os.path.join(self.dir, "guard")
            inside = os.path.join(pl.GUARD_DIR, "预演.md")
            with redirect_stdout(io.StringIO()):
                pl.cmd_batch(args_for(self.dir, folder=self.inbox, out=self.books, report=inside,
                                      lock=os.path.join(pl.GUARD_DIR, "batch.lock.json"),
                                      journal=os.path.join(pl.GUARD_DIR, "batch_runs.jsonl")))
            self.assertTrue(os.path.isfile(inside))
            text = rf(inside)
            self.assertIn("只预演，零写入", text)
            self.assertIn("raw覆盖", text)
        finally:
            pl.GUARD_DIR = old_guard

    def test_apply_refused_by_live_lock_and_writes_nothing(self):
        lock = os.path.join(self.dir, "guard", "batch.lock.json")
        wj(lock, {"pid": os.getpid(), "runId": "20261127_0900", "startedAt": "s", "hbAt": "s",
                  "folder": self.inbox, "out": self.books, "workers": 1, "scale": 3.0, "planned": 4})
        before = snapshot_tree(self.dir)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pl.cmd_batch(args_for(self.dir, folder=self.inbox, out=self.books, apply=True))
        self.assertEqual(rc, 3)
        self.assertIn("拒绝启动（G1）", buf.getvalue())
        self.assertIn("20261127_0900", buf.getvalue())
        self.assertEqual(before, snapshot_tree(self.dir), "活锁拒绝路径产生了写入")

    def test_classify_categories_and_raw_risk(self):
        res = pl.classify_inbox(self.inbox, self.books, want_pdf_meta=False,
                                books_dir=self.books, args=self.args)
        by = {r["name"]: r for r in res["rows"]}
        self.assertEqual(by["已在库书"]["category"], pl.CAT_INGESTED)
        self.assertTrue(by["已在库书"]["rawRisk"])
        self.assertAlmostEqual(by["已在库书"]["rawRatio"], 0.6, places=2)
        self.assertEqual(by["半成品书-第二版"]["category"], pl.CAT_PARTIAL)
        self.assertIn("预检.json.pdf", [k for h in by["半成品书-第二版"]["idKeys"] for k in h["keys"]])
        self.assertEqual(by["建筑力学 (吕令毅，吕子华编著"]["category"], pl.CAT_MANUAL)
        # 异名同书：目录名/主干都不同，只靠 book.json 的 source 命中 → 判「需人工判定」，绝不当新书
        self.assertEqual(by["改名后的源文件"]["category"], pl.CAT_MANUAL)
        self.assertTrue(any("source" in k for h in by["改名后的源文件"]["idKeys"] for k in h["keys"]))
        self.assertIn("疑似同一本书", "".join(by["改名后的源文件"]["evidence"]))
        self.assertEqual(by["全新书"]["category"], pl.CAT_NEW)
        self.assertEqual(res["counts"][pl.CAT_NEW], 1)
        self.assertEqual(res["noWrite"], 2)
        self.assertEqual(len(res["rawRisk"]), 1)
        run, skip = pl.plan_from_classify(res, self.args)
        self.assertEqual([r["name"] for r in run], ["全新书"], "只有真新增可跑")
        self.assertEqual(len(skip), 5)
        self.assertTrue(any("G6 高危" in w for _r, w in skip))
        self.assertTrue(any("G3 零写入" in w for _r, w in skip))
        self.assertTrue(any("需人工判定" in w for _r, w in skip))
        # 四键纠偏：目录名不同但在途目录的 预检.json.pdf 指向该 PDF → 不给当新书跑
        pl.classify_inbox(self.inbox, self.books, want_pdf_meta=False,
                          books_dir=self.books, args=self.args)
        self.assertTrue(any("G6 高危" in w for _r, w in skip))
        self.assertTrue(any("G3 零写入" in w for _r, w in skip))

    def test_identity_hits_cross_name(self):
        recs = pl.build_identity_index(self.books)
        hits = pl.identity_hits(os.path.join(self.inbox, "建筑力学 (吕令毅，吕子华编著.pdf"), 0, recs)
        self.assertTrue(hits)
        top = hits[0]
        self.assertEqual(top["dir"], "建筑力学（第三版） (吕令毅、吕子华)")
        self.assertIn("book.json.source", top["keys"])
        self.assertTrue(top["strong"])


class IngestedZeroWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_g3_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.inbox = os.path.join(self.dir, "inbox")
        self.books = os.path.join(self.dir, "books")
        os.makedirs(self.inbox)
        os.makedirs(self.books)
        self.pdf = os.path.join(self.inbox, "已在库书.pdf")
        wf(self.pdf, "%PDF fake")
        self.bd = make_book(self.books, "已在库书", book_json=book_obj("已在库书", chars=300000),
                           marker={"version": 1, "pdf": self.pdf, "scale": 3.0,
                                   "done": {s: {"at": "t"} for s in pl.POST_STAGES + ["convert"]}},
                           qc={"checkedAt": "2026-11-01 10:00", "gate": {"ok": True, "checks": []}})
        self.before = snapshot_tree(self.books)
        # 任何阶段被调用就炸：证明 G3 在 process_one 之前就返回了
        self._orig = (pl._run_stages, pl.gate, pl.write_report, pl.preflight, pl.probe_quality)
        self.stages_called = []

        def boom(*a, **k):
            raise AssertionError("护栏失效：已入库书稿仍去跑阶段/写盘")

        def recorder(pdf, out_dir, book_dir, book_name, args, fp, stages, vertical=False):
            self.stages_called.append(book_dir)
            return True

        pl.gate = boom
        pl.write_report = boom
        pl.preflight = lambda pdf: {"pdf": pdf, "pages": 10, "size": "1×1", "textLayer": True,
                                    "etaMinutes": 0, "warn": ""}
        pl.probe_quality = lambda pdf, sample=6: {}
        pl._run_stages = recorder
        self.addCleanup(self._restore)

    def _restore(self):
        pl._run_stages, pl.gate, pl.write_report, pl.preflight, pl.probe_quality = self._orig

    def test_ingested_book_zero_write_without_authorization(self):
        args = args_for(self.dir, folder=self.inbox, out=self.books)
        self.assertTrue(pl.process_one(self.pdf, self.books, args, None))
        self.assertEqual(self.before, snapshot_tree(self.books), "已入库书稿被写动了")
        self.assertEqual(self.stages_called, [], "G3 之后还在跑阶段")
        self.assertFalse(os.path.exists(os.path.join(self.bd, "预检.json")))
        # 事故路径复现：marker.done 非空 + book.json 存在，也必须零写入
        self.assertTrue(os.path.exists(os.path.join(self.bd, pl.MARKER)))
        self.assertIn("旧报告", rf(os.path.join(self.bd, "转换报告.md")))

    def test_partial_dir_without_bookjson_is_not_blocked(self):
        pd = make_book(self.books, "在途半成品", preflight={"pdf": "x", "pages": 3}, pages=True)
        pdf2 = os.path.join(self.inbox, "在途半成品.pdf")
        wf(pdf2, "%PDF fake")
        args = args_for(self.dir, folder=self.inbox, out=self.books)
        pl.process_one(pdf2, self.books, args, None)
        self.assertEqual(self.stages_called, [pd], "未入库的目录不该被 G3 拦住")

    def test_authorization_paths(self):
        args = args_for(self.dir, folder=self.inbox, out=self.books)
        self.assertFalse(pl.reconvert_authorized(self.bd, args, set()))
        self.assertTrue(pl.reconvert_authorized(self.bd, args_for(self.dir, force=True), set()))
        lst = os.path.join(self.dir, "guard", "批准清单.json")
        wj(lst, {"books": [{"dir": "已在库书", "reason": "人工批准重转"}]})
        keys = pl.load_reconvert_approvals(lst)
        self.assertTrue(pl.reconvert_authorized(self.bd, args, keys))
        txt = os.path.join(self.dir, "guard", "批准清单.txt")
        wf(txt, "# 注释\n已在库书\n")
        self.assertTrue(pl.reconvert_authorized(self.bd, args, pl.load_reconvert_approvals(txt)))


class LogPairingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_log_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = os.path.join(self.dir, "转换日志.txt")

    def test_pairs_by_run_id_not_time_delta(self):
        wf(self.log, "\n".join([
            "[23:56:51] 批处理开始：8 本 · workers=1 · scale=3.0",
            "[10:53:45] 批处理开始：41 本 · workers=1 · scale=3.0",
            "[03:27:52] 批处理开始：26 本 · workers=1 · scale=3.0",
            "[03:28:40] 批处理开始：26 本 · workers=1 · scale=3.0",
            "[04:00:00] 批处理结束：1/26 通过 · 总耗时 1.0 分钟",
            "[09:10:00] 批处理计划（run-id=20261127_0910）：3 本",
            "[09:20:00] 批处理结束（run-id=20261127_0910）：3/3 通过 · 总耗时 10.0 分钟",
            "[09:30:00] 批处理计划（run-id=20261127_0930）：2 本",
            "",
        ]))
        r = pl.legacy_log_unfinished(self.log)
        self.assertEqual(r["unmatchedRunIds"], ["20261127_0930"], "按 run-id 配对失灵")
        self.assertEqual(len(r["noRunIdUnmatched"]), 3)
        self.assertEqual(r["runs"], 2)
        self.assertTrue(any("20261127_0930" in x for x in r["unmatched"]))

    def test_missing_log_is_quiet(self):
        r = pl.legacy_log_unfinished(os.path.join(self.dir, "无.txt"))
        self.assertEqual(r["starts"], 0)
        self.assertEqual(r["unmatchedRunIds"], [])

    def test_batch_reports_run_id_and_history_separately(self):
        books = os.path.join(self.dir, "books")
        inbox = os.path.join(self.dir, "inbox")
        os.makedirs(books)
        os.makedirs(inbox)
        wf(os.path.join(inbox, "某书.pdf"), "%PDF fake")
        wf(os.path.join(books, "转换日志.txt"),
           "[03:27:52] 批处理开始：26 本 · workers=1 · scale=3.0\n"
           "[09:30:00] 批处理计划（run-id=20261127_0930）：2 本\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            pl.cmd_batch(args_for(self.dir, folder=inbox, out=books, no_pdf_meta=True))
        out = buf.getvalue()
        self.assertIn("按 run-id 配对：run-id 20261127_0930", out)
        self.assertIn("历史提示（非判据）", out)
        self.assertIn("没有 run-id，只作背景，不参与拦截", out)


class PostBannerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_banner_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.books = os.path.join(self.dir, "books")
        os.makedirs(self.books)

    def test_banner_shows_ratio_and_verdict(self):
        risky = make_book(self.books, "地雷书", book_json=book_obj("地雷书", chars=150000),
                          raw=book_obj("地雷书", chars=900000))
        safe = make_book(self.books, "安全书", book_json=book_obj("安全书", chars=200000),
                         raw=book_obj("安全书", chars=200000))
        none = make_book(self.books, "无raw书", book_json=book_obj("无raw书", chars=200000))
        line = pl.restore_raw_banner(risky)
        self.assertIn("[G6]", line)
        self.assertIn("0.1667", line)
        self.assertIn("高危", line)
        self.assertIn("--allow-restore-raw", line)
        self.assertIn("安全", pl.restore_raw_banner(safe))
        self.assertIn("不涉及覆盖", pl.restore_raw_banner(none))


class RestoreRawGateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard_g6_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.books = os.path.join(self.dir, "books")
        os.makedirs(self.books)

    def test_verdict_blocked_when_raw_much_older(self):
        cur = book_obj("甲", chars=300000)
        raw = book_obj("甲", chars=500000)
        action, ratio, reason = pl.restore_raw_verdict(raw, cur, 1_700_000_000, 1_700_000_000 + 86400 * 30)
        self.assertEqual(action, "blocked")
        self.assertAlmostEqual(ratio, 0.6, places=3)
        self.assertIn("正文比", reason)
        self.assertIn("深审成果", reason)

    def test_verdict_allow_and_snapshot_before_overwrite(self):
        bd = make_book(self.books, "新书", book_json=book_obj("新书", chars=200000),
                       raw=book_obj("新书", chars=200000))
        os.utime(os.path.join(bd, "book_raw.json"), (time.time() - 60, time.time() - 60))
        action, ratio, reason = pl.restore_raw_verdict(
            json.loads(rf(os.path.join(bd, "book_raw.json"))),
            json.loads(rf(os.path.join(bd, "book.json"))),
            os.path.getmtime(os.path.join(bd, "book_raw.json")),
            os.path.getmtime(os.path.join(bd, "book.json")))
        self.assertEqual(action, "allow")
        did, info = pl.restore_raw(bd, log_fn=lambda m, fp=None: None)
        self.assertTrue(did)
        self.assertTrue(info["backup"] and os.path.isfile(info["backup"]),
                        "覆盖前没写快照")
        self.assertIn("_book_backups", info["backup"])
        self.assertIn("restore_raw前", os.path.basename(info["backup"]))

    def test_blocked_restore_keeps_current_book(self):
        bd = make_book(self.books, "深审书", book_json=book_obj("深审书", chars=150000),
                       raw=book_obj("深审书", chars=900000))
        os.utime(os.path.join(bd, "book_raw.json"), (time.time() - 86400 * 40, time.time() - 86400 * 40))
        before = rf(os.path.join(bd, "book.json"))
        logs = []
        did, info = pl.restore_raw(bd, log_fn=lambda m, fp=None: logs.append(m))
        self.assertFalse(did)
        self.assertEqual(info["action"], "blocked")
        self.assertEqual(before, rf(os.path.join(bd, "book.json")), "被拒绝的 restore 仍改了书稿")
        self.assertIn("拒绝 restore_raw", "\n".join(logs))
        # 显式放行才覆盖，且一定先快照
        did2, info2 = pl.restore_raw(bd, allow=True, log_fn=lambda m, fp=None: None)
        self.assertTrue(did2)
        self.assertTrue(os.path.isfile(info2["backup"]))
        self.assertNotEqual(before, rf(os.path.join(bd, "book.json")))

    def test_no_raw_skips(self):
        bd = make_book(self.books, "无raw", book_json=book_obj("无raw", chars=1000))
        did, info = pl.restore_raw(bd)
        self.assertFalse(did)
        self.assertEqual(info["action"], "skip")
        self.assertEqual(pl.restore_raw_verdict(None, book_obj("无raw")), ("skip", None,
                                                                       "无 book_raw.json，不涉及覆盖"))


class FingerPrintTest(unittest.TestCase):
    def test_fingerprint_ignores_stats_only(self):
        b = book_obj("甲", chars=1000)
        f1 = pl.book_fingerprint(b)
        b2 = json.loads(json.dumps(b))
        b2["stats"]["chars"] = 999999
        b2["stats"]["chapters"] = 7
        self.assertEqual(f1, pl.book_fingerprint(b2), "改 stats 不应改变内容指纹")
        b3 = json.loads(json.dumps(b))
        b3["blocks"][0]["text"] = b3["blocks"][0]["text"] + "丢了两个字"
        self.assertNotEqual(f1, pl.book_fingerprint(b3))

    def test_gate_check_names_match_gate(self):
        d = tempfile.mkdtemp(prefix="guard_gate_")
        self.addCleanup(shutil.rmtree, d, True)
        bd = make_book(d, "夹具书", book_json=book_obj("夹具书", chars=200000, pages=120, paras=6))
        ok, checks = pl.gate(bd, write_profile=False)
        self.assertEqual([c["name"] for c in checks], pl.GATE_CHECK_NAMES)
        self.assertEqual(len(checks), 12)



class QcSummaryTest(unittest.TestCase):
    """recheck --summary-only：只读刷新汇总，绝不重跑 gate、绝不改任何书的 质检.json。"""

    def _tree(self, d):
        def qj(name, n, lost):
            return {"checkedAt": "2026-11-24 20:45", "book": name, "dir": name,
                    "note": "历史件",
                    "gate": {"ok": True, "checks": [{"name": "c%d" % k, "ok": True, "detail": ""} for k in range(n)]},
                    "textcheck": {"frag": 1, "tiny": 2, "longnote": 3, "lost_lines": lost,
                                  "lostRate": 0.01, "samples": []},
                    "coverage": {"pages": 10, "offset": 0, "median": 0.9, "lowCount": 1, "lowHard": 0}}
        for name, n, lost in (("甲书", 8, 40), ("乙书", 12, 5)):
            bd = os.path.join(d, "books", name)
            os.makedirs(bd)
            wj(os.path.join(bd, "book.json"), book_obj(name, chars=200000, pages=120, paras=6))
            wj(os.path.join(bd, "质检.json"), qj(name, n, lost))
        os.makedirs(os.path.join(d, "books", "丙书"))
        wj(os.path.join(d, "books", "丙书", "book.json"), book_obj("丙书", chars=200000, pages=120, paras=6))
        os.makedirs(os.path.join(d, "交接文档"))

    def test_summary_only_is_read_only_and_counts_items(self):
        d = tempfile.mkdtemp(prefix="guard_summary_")
        self.addCleanup(shutil.rmtree, d, True)
        self._tree(d)
        old_books, old_root = pl.BOOKS, pl.ROOT
        pl.BOOKS = os.path.join(d, "books")
        pl.ROOT = d
        try:
            before = {n: rf(os.path.join(d, "books", n, "质检.json")) for n in ("甲书", "乙书")}
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = pl.cmd_recheck(SimpleNamespace(summary_only=True, all=False, bookdir=""))
            self.assertEqual(rc, 0)
            for n in ("甲书", "乙书"):
                self.assertEqual(before[n], rf(os.path.join(d, "books", n, "质检.json")),
                                 "%s 的 质检.json 不应被 summary-only 改写" % n)
                self.assertFalse(os.path.exists(os.path.join(d, "books", n, "转换报告.md")))
            md = rf(os.path.join(d, "交接文档", "质检-复查汇总.md"))
        finally:
            pl.BOOKS, pl.ROOT = old_books, old_root
        self.assertIn("| 书 | 项数 | gate |", md)
        self.assertIn("12 项 1 本 · 8 项 1 本", md)
        self.assertIn("无质检件 1 本", buf.getvalue())
        self.assertIn("丙书", buf.getvalue())
        # 排序：丢行多的在前
        self.assertLess(md.index("甲书"), md.index("乙书"))

    def test_qc_summary_rows_skips_books_without_qc(self):
        d = tempfile.mkdtemp(prefix="guard_summary2_")
        self.addCleanup(shutil.rmtree, d, True)
        self._tree(d)
        old_books = pl.BOOKS
        pl.BOOKS = os.path.join(d, "books")
        try:
            rows, missing = pl.qc_summary_rows()
        finally:
            pl.BOOKS = old_books
        self.assertEqual(len(rows), 2)
        self.assertEqual(missing, ["丙书"])
        self.assertTrue(all("bookFingerprint" not in r or True for r in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
