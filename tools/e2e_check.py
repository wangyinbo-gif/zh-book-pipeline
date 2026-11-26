# -*- coding: utf-8 -*-
"""P1-02 真实 PDF 端到端 CI 检查器（zh-book-pipeline）。

流程（分阶段 print + 段退出码，供 CI 定位失败阶段）：
  [1/5] 渲染   夹具 PDF/expected.json 就绪（缺则调 gen_fixtures.py）+ 模型 SHA-256 复核
  [2/5] OCR    跑现有管线：横排 pipeline one / 竖排 pipeline one --vertical（--force）
  [3/5] 结构   页数 / 字数下限 / 必含字串 / CER（按页对齐参考文本，容差默认 0.35）/ 章数下限
  [4/5] gate   跑 pipeline.gate（只读 write_profile=False）——要求「能跑」，不强制全过
  [5/5] schema book.json 结构键 + stats 一致性 + 块字段健全性

退出码：
  0  全部通过
  20 渲染/夹具/模型阶段失败（fixtures 缺失、gen_fixtures 失败、模型 SHA 不符）
  21 OCR/管线阶段失败（pipeline 子进程崩溃或找不到 book.json）
  22 结构断言失败（页数/字数/字串/CER/章数）
  23 gate 无法运行（抛异常）
  24 schema 校验失败（结构键缺失/stats 不一致/块字段坏）
  其它非零 = 用法错误（argparse）

缓存与幂等：
  默认复用 <out>/<书>/pages/*_ocr2.json 与 .ocr_cache（第二次跑快）；
  环境变量 ZB_E2E_NO_CACHE=1 → 每次先清空 out 目录，全量重跑（幂等验证用）。

用法：
  python tools/e2e_check.py [--fixtures-dir DIR] [--out DIR] [--only all|horizontal|vertical]
                            [--verify-models] [--regen]
退出码即上表；stdout 每阶段以 [STAGE n/5] 开头，便于 CI 日志切片。
"""
import argparse
import difflib
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = HERE
EXIT_RENDER, EXIT_OCR, EXIT_STRUCT, EXIT_GATE, EXIT_SCHEMA = 20, 21, 22, 23, 24
CJK = re.compile(r"[\u4e00-\u9fff]")


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def stage(n, msg):
    print("[STAGE %d/5] %s" % (n, msg), flush=True)


def norm_cjk(s):
    """仅保留汉字（去标点/空格/西文数字），CER 与字串命中都按此口径。"""
    return "".join(CJK.findall(s or ""))


def cer_of(ref, got):
    """字符错误率近似：1 - difflib 相似度（对 OCR 顺序错位稳健，容差内可放行）。"""
    a, b = norm_cjk(ref), norm_cjk(got)
    if not a:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return round(1.0 - r, 4)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------- 模型复核
def _model_search_dirs():
    dirs = [os.path.join(ROOT, "models"), os.path.join(TOOLS, "models")]
    try:
        import rapidocr_onnxruntime
        dirs.append(os.path.join(os.path.dirname(rapidocr_onnxruntime.__file__), "models"))
    except Exception:
        pass
    return dirs


def verify_models(verbose=True):
    """按 MODELS.sha256 复核全部模型文件；返回 (ok, rows)。清单缺失=跳过（warn）。"""
    sha_file = os.path.join(TOOLS, "models", "MODELS.sha256")
    if not os.path.exists(sha_file):
        if verbose:
            print("  ! 无 MODELS.sha256，跳过模型复核（建议 lead 同步进仓）")
        return True, []
    rows, ok = [], True
    search = _model_search_dirs()
    for line in open(sha_file, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        want, path = parts[0], parts[1]
        base = os.path.basename(path)
        found = next((os.path.join(d, base) for d in search
                      if os.path.exists(os.path.join(d, base))), None)
        if not found:
            rows.append((base, "missing", want[:12]))
            ok = False
            continue
        got = file_sha256(found)
        rows.append((base, "ok" if got == want else "MISMATCH", got[:12]))
        if got != want:
            ok = False
        if verbose:
            print("  %s %s %s" % ("✓" if ok else "✗", base, got[:16]))
    return ok, rows


# ---------------------------------------------------------------- 阶段 1：夹具
def ensure_fixtures(fixtures_dir, regen):
    gen = os.path.join(fixtures_dir, "gen_fixtures.py")
    need = [os.path.join(fixtures_dir, n) for n in
            ("horizontal.pdf", "vertical.pdf",
             "horizontal.expected.json", "vertical.expected.json")]
    if regen or not all(os.path.exists(p) for p in need):
        if not os.path.exists(gen):
            print("  ! 夹具与生成器都不存在：%s" % gen)
            return False
        r = subprocess.run([sys.executable, gen, "--outdir", fixtures_dir],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        print((r.stdout or "").strip())
        if r.returncode != 0:
            print("  ! gen_fixtures rc=%s\n%s" % (r.returncode, (r.stderr or "")[-800:]))
            return False
    return True


# ---------------------------------------------------------------- 阶段 2：管线
def run_pipeline(pdf, out_dir, expected):
    title = expected["pipeline"]["title"]
    cmd = [sys.executable, os.path.join(TOOLS, "pipeline.py"), "one", pdf,
           "--out", out_dir, "--title", title, "--force"]
    if expected["pipeline"].get("vertical"):
        cmd.append("--vertical")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    dt = time.time() - t0
    tail = "\n".join((r.stdout or "").splitlines()[-25:])
    print("  pipeline rc=%s · %.1fs" % (r.returncode, dt))
    print(tail)
    if (r.stderr or "").strip():
        print("  stderr尾部:", r.stderr.strip()[-500:])
    return r.returncode, dt


def locate_book(out_dir, pdf):
    """book.json 位置：正常在 out/<safe名>/；gate 不过会被移入 需人工处理/未达标书稿/。"""
    cands = sorted(glob.glob(os.path.join(out_dir, "*", "book.json")))
    cands += sorted(glob.glob(os.path.join(out_dir, "需人工处理", "未达标书稿",
                                           "*", "book.json")))
    stem = os.path.splitext(os.path.basename(pdf))[0]
    for c in cands:
        if stem.lower() in c.lower():
            return os.path.dirname(c)
    return os.path.dirname(cands[0]) if cands else None


# ---------------------------------------------------------------- 阶段 3：结构
def page_texts(book):
    """按页聚合 blocks 文本；无 page 字段的块并入全文兜底。"""
    per = {}
    loose = []
    for b in book.get("blocks", []):
        t = (b.get("text") or "") + (" " + b["caption"] if b.get("caption") else "")
        p = b.get("page")
        if isinstance(p, int) and p >= 1:
            per.setdefault(p, []).append(t)
        else:
            loose.append(t)
    return {k: "\n".join(v) for k, v in per.items()}, "\n".join(loose)


def check_structure(book_dir, expected, rep):
    book = read_json(os.path.join(book_dir, "book.json"))
    if not book:
        rep["error"] = "book.json 读不到"
        return None
    stats = book.get("stats", {}) or {}
    fails = []
    if stats.get("pages") != expected["pages"]:
        fails.append("页数 %s≠%s" % (stats.get("pages"), expected["pages"]))
    if (stats.get("chars") or 0) < expected["chars_min_total"]:
        fails.append("字数 %s<%s" % (stats.get("chars"), expected["chars_min_total"]))
    if (stats.get("chapters") or 0) < expected["chapters_min"]:
        fails.append("章数 %s<%s" % (stats.get("chapters"), expected["chapters_min"]))
    per, loose = page_texts(book)
    full_norm = norm_cjk("\n".join(list(per.values()) + [loose]))
    hits = {}
    for p, strs in (expected.get("must_contain") or {}).items():
        p = int(p)
        pt = norm_cjk(per.get(p, "")) or full_norm
        for s in strs:
            ok = norm_cjk(s) in pt or norm_cjk(s) in full_norm
            hits["p%s:%s" % (p, s)] = ok
            if not ok:
                fails.append("字串未命中 p%s「%s」" % (p, s))
    cers = {}
    tol = (expected.get("cer") or {}).get("tolerance", 0.35)
    for p, ref in ((expected.get("cer") or {}).get("refs") or {}).items():
        p = int(p)
        got = per.get(p) or full_norm
        c = cer_of(ref, got)
        cers["p%d" % p] = c
        if c > tol:
            fails.append("CER p%d=%.3f>%.2f" % (p, c, tol))
    rep.update(pages=stats.get("pages"), chars=stats.get("chars"),
               chapters=stats.get("chapters"), cer=cers,
               must_contain=hits, cer_tolerance=tol)
    return fails


# ---------------------------------------------------------------- 阶段 4：gate
def run_gate(book_dir, rep):
    """返回 (ran_ok, gate_ok, checks)。gate 抛异常 → ran_ok=False（退出码 23）。"""
    sys.path.insert(0, TOOLS)
    try:
        import importlib
        pipe = importlib.import_module("pipeline")
        ok, checks = pipe.gate(book_dir, write_profile=False)
        rep["gate"] = [{"name": c["name"], "ok": c["ok"], "detail": c["detail"]}
                       for c in checks]
        for c in checks:
            print("    %s %-8s %s" % ("✓" if c["ok"] else "✗", c["name"], c["detail"]))
        return True, ok, checks
    except Exception as e:
        import traceback
        traceback.print_exc()
        rep["gate_error"] = str(e)
        return False, False, []
    finally:
        if sys.path and sys.path[0] == TOOLS:
            sys.path.pop(0)


# ---------------------------------------------------------------- 阶段 5：schema
def check_schema(book_dir, expected, rep):
    """按 convert 实际契约校验（pdf2book/vertical_book 产出不含顶层 pages、块可无 id）。"""
    book = read_json(os.path.join(book_dir, "book.json"), {}) or {}
    fails = []
    for k in expected.get("structure_keys", []):
        if k not in book:
            fails.append("缺结构键 %s" % k)
    blocks = book.get("blocks") or []
    if not blocks:
        fails.append("blocks 为空")
    bad = [i for i, b in enumerate(blocks)
           if not b.get("type") or not (b.get("text") or b.get("src"))]
    if bad:
        fails.append("块缺 type 或 text/src：块号 %s…" % bad[:3])
    ids = [b["id"] for b in blocks if "id" in b]
    if len(ids) != len(set(ids)):
        fails.append("块 id 重复")
    stats = book.get("stats") or {}
    pages = stats.get("pages")
    if pages is None:
        fails.append("stats.pages 缺失")
    else:
        over = [b for b in blocks if isinstance(b.get("page"), int)
                and (b["page"] < 1 or b["page"] > pages)]
        if over:
            fails.append("块页码越界 %d 个（1..%s）" % (len(over), pages))
        if book.get("pages") not in (None, pages):
            fails.append("pages(%s)≠stats.pages(%s)" % (book.get("pages"), pages))
    if not stats.get("chars"):
        fails.append("stats.chars 为 0")
    rep["schema_blocks"] = len(blocks)
    return fails


# ---------------------------------------------------------------- 主流程
def run_fixture(name, fixtures_dir, out_root, regen, no_cache):
    pdf = os.path.join(fixtures_dir, name + ".pdf")
    expected = read_json(os.path.join(fixtures_dir, name + ".expected.json"))
    if not expected:
        print("  ! 缺 expected.json：%s" % name)
        return EXIT_RENDER, {}
    out_dir = os.path.join(out_root, name)
    if no_cache and os.path.isdir(out_dir):
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
        print("  （ZB_E2E_NO_CACHE=1：已清空 %s 全量重跑）" % out_dir)
    os.makedirs(out_dir, exist_ok=True)
    rep = {"fixture": name, "pdf_sha256": file_sha256(pdf)}

    stage(2, "%s OCR：pipeline one --force%s" % (name, " --vertical" if expected["pipeline"].get("vertical") else ""))
    rc, dt = run_pipeline(pdf, out_dir, expected)
    rep["pipeline_rc"], rep["pipeline_seconds"] = rc, round(dt, 1)
    book_dir = locate_book(out_dir, pdf)
    if not book_dir:
        rep["error"] = "pipeline 跑完但找不到 book.json"
        print("  ! 找不到 book.json（rc=%s）" % rc)
        return EXIT_OCR, rep
    rep["book_dir"] = book_dir
    print("  book.json → %s" % book_dir)

    stage(3, "%s 结构断言（页数/字数/字串/CER/章数）" % name)
    fails = check_structure(book_dir, expected, rep)
    if fails is None:
        return EXIT_OCR, rep
    for f in fails:
        print("    ✗ %s" % f)
    if not fails:
        print("    ✓ 页数=%s 字数=%s 章数=%s CER=%s 字串全命中" % (
            rep["pages"], rep["chars"], rep["chapters"], rep["cer"]))
    else:
        rep["structure_fails"] = fails
        return EXIT_STRUCT, rep

    stage(4, "%s gate 试跑（只读，不强制全过）" % name)
    ran, gate_ok, _ = run_gate(book_dir, rep)
    if not ran:
        return EXIT_GATE, rep
    print("    gate 可运行 ✓（整体判定 %s；不过项已列上，不算 CI 失败）" %
          ("PASS" if gate_ok else "WARN"))

    stage(5, "%s schema 校验" % name)
    sfails = check_schema(book_dir, expected, rep)
    for f in sfails:
        print("    ✗ %s" % f)
    if sfails:
        rep["schema_fails"] = sfails
        return EXIT_SCHEMA, rep
    print("    ✓ 结构键 %s · 块 %d" % (expected["structure_keys"], rep["schema_blocks"]))
    try:
        import run_info
        p = run_info.write_run_info(book_dir, "e2e_ci", {
            "input": pdf, "params": {"fixture": name, "pipeline_rc": rc}})
        print("    run_info → %s" % p)
    except Exception as e:
        print("    （run_info 可选，未写：%s）" % e)
    return 0, rep


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(description="真实 PDF 端到端 CI 检查（退出码 20/21/22/23/24）")
    default_fixtures = os.environ.get("ZB_E2E_FIXTURES") or next(
        (c for c in [os.path.join(ROOT, "tests", "e2e"),
                     os.path.join(ROOT, "_工作记录", "工程V1.2_20261126", "e2e_assets")]
         if os.path.isdir(c)), os.path.join(ROOT, "tests", "e2e"))
    ap.add_argument("--fixtures-dir", default=default_fixtures)
    ap.add_argument("--out", default=os.environ.get(
        "ZB_E2E_OUT", os.path.join(ROOT, "_工作记录", "工程V1.2_20261126", "e2e_out")))
    ap.add_argument("--only", default="all", choices=["all", "horizontal", "vertical"])
    ap.add_argument("--regen", action="store_true", help="强制重渲染夹具")
    ap.add_argument("--verify-models", action="store_true", help="只做模型 SHA 复核")
    args = ap.parse_args(argv)
    no_cache = os.environ.get("ZB_E2E_NO_CACHE", "") == "1"

    stage(1, "夹具与模型就绪检查：%s" % args.fixtures_dir)
    if not os.path.isdir(args.fixtures_dir):
        print("  ! 夹具目录不存在")
        return EXIT_RENDER
    if not ensure_fixtures(args.fixtures_dir, args.regen):
        return EXIT_RENDER
    print("  夹具 OK")
    ok, rows = verify_models()
    if not ok:
        print("  ! 模型 SHA 复核失败：%s" % rows)
        return EXIT_RENDER
    if args.verify_models:
        print("  模型复核 OK（--verify-models 模式结束）")
        return 0

    names = ["horizontal", "vertical"] if args.only == "all" else [args.only]
    os.makedirs(args.out, exist_ok=True)
    report = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "no_cache": no_cache,
              "fixtures_dir": args.fixtures_dir, "results": {}}
    worst = 0
    for n in names:
        rc, rep = run_fixture(n, args.fixtures_dir, args.out, args.regen, no_cache)
        report["results"][n] = rep
        if rc and not worst:
            worst = rc
        print("  == %s → rc=%s" % (n, rc))
    rp = os.path.join(args.out, "e2e_report.json")
    with open(rp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("[done] 报告：%s · 退出码 %s" % (rp, worst))
    return worst


if __name__ == "__main__":
    sys.exit(main())
