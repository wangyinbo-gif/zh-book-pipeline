# -*- coding: utf-8 -*-
"""按清单转换指定的新书（不碰已转换的书）。

用法：
    python tools/convert_new.py --workers 2 "待转换书籍/某书.pdf" ...
    python tools/convert_new.py --workers 2 --list books/新书清单.txt

输出到 books/，日志追加到 books/新书转换日志.txt。每本完成立即打印一行结果。
"""
from paths import ROOT, BOOKS
import argparse
import concurrent.futures
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="*")
    ap.add_argument("--list", default="")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--scale", type=float, default=3.0)
    ap.add_argument("--out", default=pipeline.BOOKS)
    args = ap.parse_args()

    pdfs = list(args.pdfs)
    if args.list:
        with open(args.list, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if not os.path.isabs(line):
                    line = os.path.join(pipeline.ROOT, line)
                pdfs.append(line)
    pdfs = [p for p in pdfs if os.path.exists(p)]
    if not pdfs:
        print("没有可转换的 PDF")
        return 2

    # 页数多的先开跑（缩短总时长）
    def pages(p):
        try:
            return pipeline.pdf_page_count(p)
        except Exception:
            return 0

    pdfs.sort(key=pages, reverse=True)
    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "新书转换日志.txt")
    ns = argparse.Namespace(force=False, scale=args.scale, from_stage="", title="", out=args.out)
    t0 = time.time()
    results = []
    with open(log_path, "a", encoding="utf-8") as fp:
        pipeline.log("=== 新书批处理开始：%d 本 · workers=%d · scale=%s" % (len(pdfs), args.workers, args.scale), fp)
        for p in pdfs:
            pipeline.log("   待转：%s（%d 页）" % (os.path.basename(p)[:60], pages(p)), fp)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(pipeline.process_one, p, args.out, ns, fp): p for p in pdfs}
            for fut in concurrent.futures.as_completed(futs):
                p = futs[fut]
                try:
                    ok = fut.result()
                except Exception as e:
                    pipeline.log("  ! %s 异常：%s" % (os.path.basename(p), e), fp)
                    ok = False
                results.append((p, ok))
                pipeline.log("--- 完成 %s：%s（累计 %.1f 分钟）" % (
                    os.path.basename(p)[:40], "✅ 通过" if ok else "⚠️ 需人工", (time.time() - t0) / 60), fp)
        okn = sum(1 for _, ok in results if ok)
        pipeline.log("=== 新书批处理结束：%d/%d 通过 · 总耗时 %.1f 分钟" % (okn, len(results), (time.time() - t0) / 60), fp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
