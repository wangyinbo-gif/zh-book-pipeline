"""Exercise the normal surgery entry point with an isolated copy of the library."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import page_surgery as surgery
import pipeline
import repair_preflight as preflight


class RepairPreflightTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        library = patch.object(preflight, "ROOT", self.root)
        library.start()
        self.addCleanup(library.stop)
        self.book_dir = self.root / "books" / "精确书名"
        self.book_dir.mkdir(parents=True)
        self.book = self.book_dir / "book.json"
        self.plan = self.root / "plan.json"
        self.approval = self.root / "approval.json"
        self.evidence = self.root / "p2.txt"
        self.evidence.write_text("对照页来源图注", encoding="utf8")
        self.payload = {"chapters": [], "stats": {}, "blocks":[
            {"type":"note","page":2,"text":"本页注明依据全书资料"},
            {"type":"heading","page":2,"text":"本节标题"},
            {"type":"figure","id":"fig-001","page":2,"caption":"旧图注"}
        ]}
        self.raw = json.dumps(self.payload, ensure_ascii=False, indent=1).replace("\n","\r\n").encode("utf8")
        self.book.write_bytes(self.raw)
        self.spec = {"book_dir":str(self.book_dir),"expected_book_sha256":preflight.digest(self.raw),"apply":True,
                     "regions":[{"page":2,"start_prefix":"旧图注","last_prefix":"旧图注",
                                 "blocks":[{"$keep_figure":"fig-001","caption":"对照页来源图注"}]}]}
        self.save_plan()

    def save_plan(self):
        self.plan.write_text(json.dumps(self.spec,ensure_ascii=False),encoding="utf8")

    def invoke(self, *args):
        out, err = StringIO(), StringIO()
        with patch("sys.argv",["page_surgery.py",str(self.plan),*args]), redirect_stdout(out), redirect_stderr(err):
            result = surgery.main()
        return result,out.getvalue(),err.getvalue()

    def review(self):
        result,out,_ = self.invoke("--describe")
        self.assertEqual(result,0)
        receipt = json.loads(out[out.index("{"):out.rindex("}")+1])
        receipt.update(approved_by="验收样例",
                       evidence=[{"path":"p2.txt","sha256":preflight.digest(self.evidence.read_bytes()),"pages":[2]}])
        self.approval.write_text(json.dumps(receipt,ensure_ascii=False),encoding="utf8")

    def test_live_write_requires_approval_and_preserves_format(self):
        rc,_,err=self.invoke()
        self.assertEqual(rc,1)
        self.assertIn("--approval",err)
        self.assertEqual(self.book.read_bytes(), self.raw)
        self.review()
        rc,_,err=self.invoke("--approval",str(self.approval))
        self.assertEqual((rc,err),(0,""))
        self.assertEqual(json.loads(self.book.read_bytes())["blocks"][-1]["caption"],"对照页来源图注")
        self.assertGreater(self.book.read_bytes().count(b"\r\n"),0)
        self.assertEqual(next(self.book_dir.glob("book.json.bak*")).read_bytes(),self.raw)

    def test_existing_approval_rejects_undeclared_89_figures_and_stale_evidence(self):
        self.review()
        self.spec["regions"][0]["blocks"].extend(
            {"type":"figure","page":2,"id":f"extra-{i}","caption":"新增"} for i in range(89))
        self.save_plan()
        rc,_,err=self.invoke("--approval",str(self.approval))
        self.assertEqual(rc,1)
        self.assertIn("does not match",err)
        del self.spec["regions"][0]["blocks"][1:]
        self.save_plan()
        self.evidence.write_text("前件已修改",encoding="utf8")
        rc,_,err=self.invoke("--approval",str(self.approval))
        self.assertEqual(rc,1)
        self.assertIn("source evidence",err)
        self.assertEqual(self.book.read_bytes(),self.raw)

    def test_heading_loss_blocks_before_writing(self):
        self.spec["regions"][0].update(start_prefix="本节标题")
        self.save_plan()
        rc,_,err=self.invoke("--describe")
        self.assertEqual(rc,1)
        self.assertIn("role regression",err)
        self.assertEqual(self.book.read_bytes(),self.raw)

    def test_release_holds_require_exact_identity(self):
        f=self.root / preflight.HOLD_FILE
        f.parent.mkdir(parents=True)
        name=self.book_dir.name
        f.write_text(json.dumps({"held":[{"book":name,"status":"待核"}]},ensure_ascii=False),encoding="utf8")
        self.assertEqual(preflight.release_holds(), {name:"待核"})
        f.write_text(json.dumps({"held":[{"book":name[:2],"status":"待核"}]},ensure_ascii=False),encoding="utf8")
        with self.assertRaises(ValueError):
            preflight.release_holds()

    def test_sliding_grams_and_cross_page_short_line(self):
        t1,t2="甲乙丙丁戊己庚辛","天地玄黄宇宙洪荒"
        self.assertIn("乙丙丁戊",pipeline.p0_grams(t1))
        (self.book_dir/"pages").mkdir()
        for page,data in ((1,t1),(2,t2)):
            (self.book_dir/"pages"/f"page_{page:03}_ocr2.json").write_text(
                json.dumps([{"text":data}],ensure_ascii=False),encoding="utf8")
        b={"type":"paragraph","page":1,"pages":[1,2],"text":t1+t2}
        self.book.write_text(json.dumps({"blocks":[b],"stats":{"pages":2}},ensure_ascii=False),encoding="utf8")
        d=pipeline.textcheck(str(self.book_dir))
        self.assertEqual(d["no_block_pages"],[])
        self.assertEqual([r["page"] for r in d["short_continuation_candidates"]],[1,2])
        self.assertEqual(pipeline.p0_source_counts([b],{1:t1,2:t2},str),({1:8,2:8},0))
