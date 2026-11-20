# -*- coding: utf-8 -*-
"""Combine independently reviewed page-surgery regions against one book snapshot.

Usage: python tools/review_batch.py manifest.json --out output_directory [--ready --dry-plan PLAN]

Each item names an existing page_surgery plan and supplies one review per region:
  {"book_dir": "D:/.../books/book", "items": [
    {"plan": "plan.json", "reviews": [
      {"evidence": ["p001.jpg"], "verified": false,
       "deletion_explanation": ""}
    ]}
  ]}
Paths in the manifest are resolved relative to the manifest. The generated
plan is dry-run by default. --ready only generates apply:true when each region
has a recorded manual verification, existing evidence, an explanation for any
net removed Chinese characters, and all figure assets exist. This tool never
applies a plan and cannot determine whether a human actually reviewed it.
"""
import argparse
import hashlib
import json
from pathlib import Path
from page_surgery import bag, block_text


def resolve(base, value):
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def describe(block):
    result = {k: block[k] for k in ("type", "page", "id", "src", "text", "caption", "rows") if k in block}
    return result


def difference(before, after):
    deleted = bag(before) - bag(after)
    added = bag(after) - bag(before)
    return dict(sorted(deleted.items())), dict(sorted(added.items()))


def materialize(specs, blocks, page):
    out = []
    for spec in specs:
        if spec.get("$keep_figure"):
            matches = [b for b in blocks if b.get("type") == "figure" and b.get("id") == spec["$keep_figure"]]
            if len(matches) != 1:
                raise ValueError("图块 ID 不唯一或不存在: " + str(spec["$keep_figure"]))
            b = dict(matches[0])
            b.update({k: v for k, v in spec.items() if k != "$keep_figure"})
            b["type"] = "figure"
        else:
            b = dict(spec)
            b.setdefault("page", page)
            b.setdefault("col", 0)
        out.append(b)
    return out


def compile_batch(manifest_file, ready=False):
    manifest_file = Path(manifest_file).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
    base = manifest_file.parent
    book_dir = resolve(base, manifest["book_dir"])
    raw = (book_dir / "book.json").read_bytes()
    book = json.loads(raw.decode("utf-8-sig"))
    blocks = book["blocks"]
    sha = hashlib.sha256(raw).hexdigest()
    if not manifest.get("items"):
        raise ValueError("计划列表为空")
    regions, details, failures = [], [], []
    for item in manifest["items"]:
        plan_path = resolve(base, item["plan"])
        plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        if resolve(plan_path.parent, plan["book_dir"]) != book_dir:
            raise ValueError("计划的书目录与清单不一致: " + str(plan_path))
        specs = plan.get("regions") or []
        reviews = item.get("reviews") or []
        if len(specs) != len(reviews):
            raise ValueError("每个区域都必须对应一条核验记录: " + str(plan_path))
        for reg, review in zip(specs, reviews):
            page = reg["page"]
            starts = [i for i, b in enumerate(blocks) if b.get("page") == page and block_text(b).startswith(reg["start_prefix"])]
            if len(starts) != 1:
                raise ValueError("起点不唯一或不存在: %s p%s" % (plan_path, page))
            start = starts[0]
            if reg.get("last_prefix"):
                ends = [i for i, b in enumerate(blocks) if block_text(b).startswith(reg["last_prefix"])]
                if len(ends) != 1 or ends[0] < start:
                    raise ValueError("终点不唯一、不存在或位于起点之前: %s p%s" % (plan_path, page))
                end = ends[0]
            else:
                end = start
                while end + 1 < len(blocks) and blocks[end + 1].get("page") == page:
                    end += 1
            proposed = materialize(reg["blocks"], blocks, page)
            removed, added = difference(blocks[start:end + 1], proposed)
            evidence = [resolve(base, path) for path in review.get("evidence", [])]
            missing_evidence = [str(path) for path in evidence if not path.is_file()]
            missing_assets = []
            for b in proposed:
                if b.get("type") == "figure":
                    src = b.get("src", "")
                    asset = (book_dir / src).resolve()
                    if not src or not asset.is_relative_to(book_dir) or not asset.is_file():
                        missing_assets.append(src or "（缺少 src）")
            problems = []
            if review.get("verified") is not True:
                problems.append("尚未标记人工核验")
            if not evidence or missing_evidence:
                problems.append("原页证据缺失或路径无效")
            if removed and not str(review.get("deletion_explanation", "")).strip():
                problems.append("净删汉字缺少逐项核实说明")
            if missing_assets:
                problems.append("图片文件缺失或位于书目录之外")
            failures.extend("p%s: %s" % (page, x) for x in problems)
            regions.append((start, end, reg))
            details.append({"page": page, "start": start, "end": end, "plan": str(plan_path),
                            "verified": review.get("verified") is True, "evidence": [str(x) for x in evidence],
                            "missing_evidence": missing_evidence, "missing_assets": missing_assets,
                            "removed_hanzi": removed, "added_hanzi": added,
                            "deletion_explanation": review.get("deletion_explanation", ""),
                            "before": [describe(x) for x in blocks[start:end + 1]],
                            "after": [describe(x) for x in proposed], "issues": problems})
    regions.sort(key=lambda x: x[0])
    for a, b in zip(regions, regions[1:]):
        if a[1] >= b[0]:
            raise ValueError("区域重叠: 块 %d..%d 和 %d..%d" % (a[0], a[1], b[0], b[1]))
    if ready and failures:
        raise ValueError("不能生成可应用计划：\n" + "\n".join(failures))
    merged = {"book_dir": str(book_dir), "expected_book_sha256": sha,
              "apply": bool(ready), "regions": [reg for _, _, reg in regions]}
    report = {"book_dir": str(book_dir), "book_sha256": sha, "ready": bool(ready),
              "region_count": len(regions), "issues": failures, "regions": details,
              "note": "字符计数含表格单元格和图注；不是逐页核审或文字顺序正确的证明。"}
    return merged, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--out", required=True)
    parser.add_argument("--ready", action="store_true")
    parser.add_argument("--dry-plan", help="--ready 时指定已生成并试算过的批量试算计划")
    args = parser.parse_args()
    merged, report = compile_batch(args.manifest, ready=args.ready)
    if args.ready:
        if not args.dry_plan:
            raise SystemExit("生成可应用计划前，请先生成并试算批量计划，再通过 --dry-plan 指定它。")
        dry = json.loads(Path(args.dry_plan).read_text(encoding="utf-8-sig"))
        expected_dry = dict(merged, apply=False)
        if dry != expected_dry:
            raise SystemExit("试算计划与当前书稿或区域不一致；请重新生成并试算。")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    name = "已核批量计划.json" if args.ready else "批量试算计划.json"
    paths = [out / name, out / ("已核区域报告.json" if args.ready else "区域试算报告.json")]
    if any(p.exists() for p in paths):
        raise SystemExit("输出文件已存在，请使用新目录，避免覆盖核验证据。")
    for path, data in zip(paths, (merged, report)):
        with path.open("x", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    print("已生成 %d 个区域；%s；人工核验待处理 %d 项" % (report["region_count"], "可应用计划" if args.ready else "只试算", len(report["issues"])))
    print("计划:", paths[0], "\n逐区域报告:", paths[1])
    print("下一步：逐区域核原页、审阅增删并运行 page_surgery；修改后按项目流程验收。")


if __name__ == "__main__":
    main()
