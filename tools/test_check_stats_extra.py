# -*- coding: utf-8 -*-
"""tools/test_check_stats_extra.py — check_stats.py 增补校验（C1–C4）的回归测试。

全部用临时目录夹具，绝不读真实 books/，也不改任何真实文档。运行：
    python -m unittest tools.test_check_stats_extra     （或 python tools/test_check_stats_extra.py）
"""
import datetime as dt
import io
import subprocess
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pipeline as pl       # noqa: E402
import project_stats as ps  # noqa: E402
import check_stats as cs    # noqa: E402


def wf(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def wj(path, obj):
    wf(path, json.dumps(obj, ensure_ascii=False))


def rf(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def book(title, chars=None, pages=100, paras=2):
    blocks = [{"type": "paragraph", "text": "正" * 500, "page": i + 1} for i in range(paras)]
    blocks.append({"type": "heading", "text": "第一章", "page": 1})
    b = {"bookTitle": title, "source": "%s.pdf" % title, "blocks": blocks,
         "chapters": [{"title": "第一章", "startBlock": 1, "chars": 900}],
         "stats": {"pages": pages, "paragraphs": paras, "figures": 0, "paired": 0, "chapters": 1}}
    b["stats"]["chars"] = pl.count_chars(b) if chars is None else chars
    return b


def fresh_qc(checks=12, fingerprint=None, minutes_ago=-5):
    """checkedAt 晚于 book.json 落盘（刚复查完），免得夹具本身被当成陈旧质检。"""
    return qc((dt.datetime.now() - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M"),
              checks, fingerprint)


def qc(checked_at, checks=12, fingerprint=None):
    d = {"checkedAt": checked_at, "gate": {"ok": True, "checks": [{"name": "n%d" % i, "ok": True,
                                                                  "detail": ""} for i in range(checks)]}}
    if fingerprint:
        d["bookFingerprint"] = fingerprint
    return d


def opts(root, **kw):
    o = SimpleNamespace(only=None, readme=os.path.join(root, "README.md"), ledger=None,
                        grace=120, today=dt.date.today(), docs=["README.md"], verbose=False,
                        show_warn=True, warn_only=False)
    o.__dict__.update(kw)
    return o


def run(root, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code, findings, info = cs.run_checks(root, opts(root, **kw))
    return code, findings, info, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp(prefix="cs_extra_")
        self.addCleanup(shutil.rmtree, d, True)
        self.root = d
        self.books = os.path.join(d, "books")
        self.good = book("正常书")
        self.make("正常书", self.good, fresh_qc())
        self.stats = ps.compute(d)
        self.sync_readme()

    def make(self, name, b, q=None, bump_book=False):
        bd = os.path.join(self.books, name)
        os.makedirs(bd, exist_ok=True)
        wj(os.path.join(bd, "book.json"), b)
        if q is not None:
            wj(os.path.join(bd, "质检.json"), q)
            if bump_book:
                t = os.path.getmtime(os.path.join(bd, "质检.json")) + 3600
                os.utime(os.path.join(bd, "book.json"), (t, t))
        return bd

    def sync_readme(self):
        """把 README 的统计块刷成现算，保证 readme 判据不干扰其它检查。"""
        rp = os.path.join(self.root, "README.md")
        wf(rp, "# 标题\n实盘规模（旧）：x\n")
        ps.apply_readme(rp, ps.compute(self.root), bootstrap=True)
        return rp


class CharsDriftTest(Base):
    def test_stale_stats_chars_fails(self):
        self.make("陈旧书", book("陈旧书", chars=self.good["stats"]["chars"] - 5000),
                  fresh_qc())
        code, findings, info, _out = run(self.root, only={"chars"})
        self.assertEqual(code, 1)
        self.assertEqual(info["chars"]["stale"], 1)
        self.assertEqual(info["chars"]["delta"], 5000)
        self.assertTrue(any(f["key"] == "陈旧书" and "count_chars" in f["msg"] for f in findings))

    def test_consistent_passes_and_warn_only(self):
        code, findings, info, _out = run(self.root, only={"chars"})
        self.assertEqual(code, 0)
        self.assertEqual(info["chars"]["stale"], 0)
        self.make("又陈旧书", book("又陈旧书", chars=1), fresh_qc())
        code2, f2, info2, _ = run(self.root, only={"chars"}, warn_only=True)
        self.assertEqual(code2, 0)
        self.assertTrue(any(x["severity"] == "fail" for x in f2))


class QcFreshnessTest(Base):
    def test_stale_qc_without_ledger_fails(self):
        self.make("旧质检书", book("旧质检书"), fresh_qc(), bump_book=True)
        code, findings, info, _out = run(self.root, only={"qc"})
        self.assertEqual(code, 1)
        self.assertEqual(info["ledger"]["state"], "missing")
        self.assertTrue([f for f in findings if f["key"] == "旧质检书" and f["severity"] == "fail"])
        self.assertIn("不在 stats-only 台账", findings[0]["msg"])

    def test_ledger_hit_downgrades_to_warn(self):
        self.make("旧质检书", book("旧质检书"), fresh_qc(), bump_book=True)
        wj(os.path.join(self.root, "reports", "stats_fix.json"),
           {"runId": "20261127_0913", "books": [{"dir": "旧质检书", "oldChars": 1,
                                                 "newChars": self.good["stats"]["chars"],
                                                 "backup": "_book_backups/旧质检书/book.json.bak_x"}]})
        code, findings, info, _out = run(self.root, only={"qc"})
        self.assertEqual(code, 0, "台账命中却仍 FAIL：%s" % findings)
        self.assertEqual(info["ledger"]["entries"], 1)
        f = [x for x in findings if x["key"] == "旧质检书"][0]
        self.assertEqual(f["severity"], "warn")
        self.assertIn("stats-only 台账", f["msg"])

    def test_fingerprint_equal_warn_differs_fail(self):
        b = book("指纹书")
        bd = self.make("指纹书", b, None)
        wj(os.path.join(bd, "质检.json"), fresh_qc(fingerprint=pl.book_fingerprint(b)))
        # 只改 stats.chars（台账没有这本书）→ 指纹一致 → WARN
        b2 = json.loads(json.dumps(b))
        b2["stats"]["chars"] = b2["stats"]["chars"] + 12345
        wj(os.path.join(bd, "book.json"), b2)
        t = os.path.getmtime(os.path.join(bd, "质检.json")) + 3600
        os.utime(os.path.join(bd, "book.json"), (t, t))
        code, findings, info, _out = run(self.root, only={"qc"})
        self.assertEqual(code, 0, findings)
        self.assertEqual([x for x in findings if x["key"] == "指纹书"][0]["severity"], "warn")
        # 改内容 → 指纹变 → FAIL
        b3 = json.loads(json.dumps(b2))
        b3["blocks"][0]["text"] = "被改过的正文"
        wj(os.path.join(bd, "book.json"), b3)
        os.utime(os.path.join(bd, "book.json"), (t + 60, t + 60))
        code2, f2, _i2, _o2 = run(self.root, only={"qc"})
        self.assertEqual(code2, 1)
        self.assertIn("内容指纹已变", [x for x in f2 if x["key"] == "指纹书"][0]["msg"])

    def test_missing_qc_and_old_gate_count(self):
        self.make("无质检书", book("无质检书"), None)
        self.make("旧版项数", book("旧版项数"), qc("2026-01-01 10:00", checks=8), bump_book=True)
        wj(os.path.join(self.root, "reports", "stats_fix.json"), {"books": [{"dir": "旧版项数"}]})
        code, findings, _info, _out = run(self.root, only={"qc"})
        self.assertEqual(code, 1)
        self.assertTrue([f for f in findings if f["key"] == "无质检书" and "缺 质检.json" in f["msg"]])
        warn = [f for f in findings if f["key"] == "旧版项数"][0]
        self.assertEqual(warn["severity"], "warn")
        self.assertIn("建议重跑 recheck", warn["msg"])   # 8 项 vs 当前 12 项


class DocsNumberTest(Base):
    def test_stale_library_totals_fail(self):
        rp = os.path.join(self.root, "README.md")
        text = rf(rp).splitlines()
        text.insert(0, "## 实盘规模")
        text.insert(1, "实盘规模（手工写的）：**%d 本 / 14,871,019 字 / %d 章 / 16,786 图（配注 15,425）**"
                    % (self.stats["books"], self.stats["chapters"]))
        wf(rp, "\n".join(text))
        code, findings, _info, _out = run(self.root, only={"docs"},
                                         today=dt.date(2026, 11, 27))
        self.assertEqual(code, 1)
        msgs = " ".join(f["msg"] for f in findings)
        self.assertIn("全库总量数字过期", msgs)
        self.assertIn("14,871,019", msgs)
        self.assertIn("配注 15,425", msgs)

    def test_per_book_subset_numbers_not_flagged(self):
        rp = os.path.join(self.root, "README.md")
        body = ("## 实盘规模\n"
                "| 主库书稿 | **%d 本（教材 1、扩展 %d）· %s 页 · %s 字 · %d 章 · %s 图（配注 %s）** |\n"
                "- 逐页人工目视完成 11 本；另有 5 本升级 12 项记录。\n"
                "- gate 12/12 通过；该书 325,851 字 / 27 页 / 25 章。\n"
                "- 历史值：44 本快照＝632 章。\n" % (self.stats["books"], self.stats["books"] - 1,
                                                format(self.stats["pages"], ","),
                                                format(self.stats["characters"], ","),
                                                self.stats["chapters"], format(self.stats["figures"], ","),
                                                format(self.stats["paired_figures"], ",")))
        wf(rp, body)
        code, findings, _info, out = run(self.root, only={"docs"}, today=dt.date(2026, 11, 27))
        self.assertEqual(code, 0, "误报子集/逐本数字：%s" % [f["msg"] for f in findings if f["severity"] == "fail"])
        self.assertNotIn("325,851", out)
        self.assertNotIn("632 章", out)

    def test_future_date_respects_alias_table(self):
        rp = os.path.join(self.root, "README.md")
        wf(rp, "## 实盘规模\n现状速览：正文写「2026-12-01 完成」，另有证书到期 2027-05-23（不自动续）。\n")
        code, findings, _info, _out = run(self.root, only={"docs"}, today=dt.date(2026, 11, 27))
        self.assertEqual(code, 1)
        self.assertIn("未来日期", " ".join(f["msg"] for f in findings))
        wf(rp, "## 实盘规模\n### 批次别名表\n2026-12-01 ↔ 2026-11-27\n正文写「2026-12-01 完成」。\n")
        code2, f2, _i2, o2 = run(self.root, only={"docs"}, today=dt.date(2026, 11, 27))
        self.assertEqual(code2, 0, f2)                      # 有别名表 → 不 FAIL
        self.assertIn("按别名处理", o2)
        # 到期语义行不计未来日期
        wf(rp, "## 实盘规模\n服务到期 2027-05-23（有效期一年）。\n")
        code3, f3, _i3, o3 = run(self.root, only={"docs"}, today=dt.date(2026, 11, 27))
        self.assertEqual(code3, 0, f3)
        self.assertIn("到期/有效期", o3)

    def test_self_declared_update_time_in_future_fails(self):
        rp = os.path.join(self.root, "README.md")
        wf(rp, "## 实盘规模\n最后更新：2030-01-01 10:00\n")
        code, findings, _info, _out = run(self.root, only={"docs"}, today=dt.date(2026, 11, 27))
        self.assertEqual(code, 1)
        self.assertIn("自称「最后更新」时点", " ".join(f["msg"] for f in findings))


class PointerTest(Base):
    def test_missing_section_and_dead_link_fail(self):
        d = os.path.join(self.root, "交接文档")
        os.makedirs(d, exist_ok=True)
        wf(os.path.join(d, "交接文档.md"), "# 文档\n\n## 一、目标\n讲目标\n\n## 二、流程\n讲流程\n")
        wf(os.path.join(d, "转换流程.md"), "# 流程\n\n## 一、跑转换\n阶段规则见交接文档.md §99。\n[旧文](不存在的文件.md)\n")
        rp = os.path.join(self.root, "README.md")
        wf(rp, "普通说明文字。\n")
        code, findings, _info, _out = run(self.root, only={"pointer"},
                                          docs=["交接文档/转换流程.md", "交接文档/交接文档.md"])
        self.assertEqual(code, 1)
        msgs = " ".join(f["msg"] for f in findings)
        self.assertIn("§99", msgs)
        self.assertIn("链接的文件不存在", msgs)

    def test_self_declared_stale_pointer_not_failed(self):
        rp = os.path.join(self.root, "README.md")
        wf(rp, "旧指针 §4/§5/§11/§17 属精简前编号，已失效。\n")
        code, findings, _info, _out = run(self.root, only={"pointer"})
        self.assertEqual(code, 0, findings)

    def test_pointer_topic_mismatch(self):
        d = os.path.join(self.root, "交接文档")
        os.makedirs(d, exist_ok=True)
        wf(os.path.join(d, "交接文档.md"),
           "# 文档\n\n## 一、项目目标\n讲目标\n\n## 二、不要做的事\n讲禁令\n")
        wf(os.path.join(d, "转换流程.md"), "# 流程\n\n## 一、跑转换\n阶段顺序、自动跳过/重试/隔离规则见交接文档.md §2。\n")
        code, findings, _info, _out = run(self.root, only={"pointer"},
                                         docs=["交接文档/转换流程.md", "交接文档/交接文档.md"])
        self.assertEqual(code, 1)
        self.assertIn("里没有这些内容", " ".join(f["msg"] for f in findings))

    def test_missing_doc_self_report(self):
        rp = os.path.join(self.root, "README.md")
        wf(rp, "旧版全文备份在（历史备份，现已不存在）。\n")
        code, findings, _info, _out = run(self.root, only={"pointer"})
        self.assertEqual(code, 1)
        self.assertIn("指针悬空", " ".join(f["msg"] for f in findings))


class ReadmeBlockTest(Base):
    def test_readme_block_still_enforced(self):
        code, findings, _info, _out = run(self.root, only={"readme"})
        self.assertEqual(code, 0)
        rp = os.path.join(self.root, "README.md")
        wf(rp, rf(rp).replace('"chapters": %d' % self.stats["chapters"], '"chapters": 9999'))
        code2, f2, _i2, _o2 = run(self.root, only={"readme"})
        self.assertEqual(code2, 1)
        self.assertTrue([f for f in f2 if f["key"] == "chapters"])

    def test_cli_exit_codes(self):
        py = sys.executable
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        script = os.path.join(HERE, "check_stats.py")
        r = subprocess.run([py, "-X", "utf8", script, "--root", self.root, "--docs", "README.md",
                           "--only", "readme,chars,qc"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OK", r.stdout)
        self.make("坏书", book("坏书", chars=1), fresh_qc())
        r2 = subprocess.run([py, "-X", "utf8", script, "--root", self.root, "--docs", "README.md",
                            "--only", "chars"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r2.returncode, 1, r2.stdout)
        r3 = subprocess.run([py, "-X", "utf8", script, "--root", self.root, "--docs", "README.md",
                            "--only", "chars", "--warn-only"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env)
        self.assertEqual(r3.returncode, 0, r3.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
