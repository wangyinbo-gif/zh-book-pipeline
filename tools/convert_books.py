#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
筑学 · 批量把 PDF 转成书稿包

用法（一般双击 一键转换书籍.bat 即可）：
    python tools/convert_books.py                # 转换「待转换书籍」里所有 PDF
    python tools/convert_books.py --force        # 重新转换（覆盖已有结果）
    python tools/convert_books.py --keep-pages   # 保留中间页图片（占磁盘，调试用）
    python tools/convert_books.py --from-page 23 --to-page 41   # 只转指定页范围

输出：
    books/<书名>/preview.html   翻页阅读器（双击就能看）
    books/<书名>/book.json      书稿数据（正文/图注/图号映射）
    books/<书名>/assets/*.webp  裁出来的插图
    books/<书名>/report.json    质量报告
"""
import os
import re
import sys
import glob
import json
import time
import shutil
import argparse
import subprocess

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF2BOOK = os.path.join(ROOT, "tools", "pdf2book.py")


def safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return name[:60] or "book"


def page_count(pdf_path: str) -> int:
    try:
        import pypdfium2 as pdfium

        return len(pdfium.PdfDocument(pdf_path))
    except Exception:
        return 0


def convert_one(pdf_path, out_root, args):
    title = os.path.splitext(os.path.basename(pdf_path))[0]
    book_dir = os.path.join(out_root, safe_name(title))
    book_json = os.path.join(book_dir, "book.json")

    if os.path.exists(book_json) and not args.force:
        print(f"⏭  跳过（已转换）：{title}")
        return "skip"

    total = page_count(pdf_path) or 0
    start = args.from_page or 1
    end = args.to_page or total
    if total and end > total:
        end = total
    print(f"\n📖 开始转换：{title}（共 {total or '?'} 页，本次 {start}-{end}）")
    t0 = time.time()

    cmd = [sys.executable, PDF2BOOK, pdf_path, str(start), str(end), book_dir, "--title", title, "--book", title]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(cmd, cwd=ROOT, env=env)
    if proc.returncode != 0:
        print(f"❌ 转换失败：{title}")
        return "fail"

    # 后处理：修书名、去目录页、重建章节
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from bookfix import fix_book
        import json as _json
        _bj = os.path.join(book_dir, "book.json")
        with open(_bj, encoding="utf-8") as _f:
            _book = _json.load(_f)
        _book = fix_book(_book)
        with open(_bj, "w", encoding="utf-8") as _f:
            _json.dump(_book, _f, ensure_ascii=False, indent=2)
        print("  后处理：章节 %s · 去掉目录页 %s" % (len(_book["chapters"]), _book["_fix"]["removedTocPages"]))
    except Exception as _e:
        print("  后处理跳过：", _e)

    report_path = os.path.join(book_dir, "report.json")
    report = {}
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    print(f"✅ 完成：{title} · {report.get('chars', 0)} 字 · {report.get('figures', 0)} 图 · "
          f"耗时 {round(time.time() - t0, 1)} 秒")

    if not args.keep_pages:
        pages_dir = os.path.join(book_dir, "pages")
        if os.path.isdir(pages_dir):
            # 只删大图（占空间），保留 OCR 缓存以便断点续跑
            freed = 0
            for root, _dirs, files in os.walk(pages_dir):
                for fn in files:
                    if fn.lower().endswith(".png"):
                        p = os.path.join(root, fn)
                        try:
                            freed += os.path.getsize(p)
                            os.remove(p)
                        except OSError:
                            pass
            print(f"🧹 已清理中间页图片，释放 {round(freed / 1024 / 1024, 1)} MB（保留 OCR 缓存）")
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=os.path.join(ROOT, "待转换书籍"))
    ap.add_argument("--output", default=os.path.join(ROOT, "books"))
    ap.add_argument("--from-page", type=int, default=1)
    ap.add_argument("--to-page", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-pages", action="store_true")
    args = ap.parse_args()

    in_dir = os.path.abspath(args.input)
    out_root = os.path.abspath(args.output)
    os.makedirs(in_dir, exist_ok=True)
    os.makedirs(out_root, exist_ok=True)

    found = {os.path.normcase(p): p for p in (glob.glob(os.path.join(in_dir, "*.pdf")) + glob.glob(os.path.join(in_dir, "*.PDF")))}
    pdfs = sorted(found.values())
    if not pdfs:
        print(f"没有找到 PDF。\n请把 PDF 放进：{in_dir}")
        return

    print(f"发现 {len(pdfs)} 个 PDF，输入目录：{in_dir}")
    print(f"输出目录：{out_root}")
    stats = {"ok": 0, "skip": 0, "fail": 0}
    for pdf in pdfs:
        try:
            stats[convert_one(pdf, out_root, args)] += 1
        except Exception as e:
            print(f"❌ {os.path.basename(pdf)} 出错：{e}")
            stats["fail"] += 1

    print("\n" + "=" * 50)
    print(f"全部结束：成功 {stats['ok']} · 跳过 {stats['skip']} · 失败 {stats['fail']}")
    if stats["ok"] or stats["skip"]:
        print(f"打开输出目录看结果：{out_root}")


if __name__ == "__main__":
    main()
