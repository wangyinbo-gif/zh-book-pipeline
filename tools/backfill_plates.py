# -*- coding: utf-8 -*-
r"""整页图版补图：书影页/插图页按页裁成 figure 块（每页 1 张）。

适用：「整页书影/图版页，页面无正文、OCR 只剩乱码」的影印书。
与 backfill_figures.py 的区别：不做 OCR 图注锚定，整页取墨迹主体当图。

用法：
  python tools\backfill_plates.py --book "<书目录>" --pdf "<pdf>" --pages 4-25,243-262 --caption "图版" --dry
  python tools\backfill_plates.py ... --apply

纪律：apply 备份 book.json.bak_<时间>_补图版前；只插 figure 块，不动既有块。
"""
import argparse, json, os, re, shutil, sys, time
import numpy as np
from PIL import Image, ImageFilter

PAD = 24

def now_tag(): return time.strftime("%Y%m%d_%H%M")

def parse_pages(s):
    out = []
    for part in s.replace("，", ",").split(","):
        part = part.strip()
        if not part: continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))

def ink_bbox(img):
    g = np.asarray(img.convert("L"), dtype=np.float32)
    ink = g < 205
    if ink.sum() < 800: return None
    rows = np.where(ink.any(axis=1))[0]; cols = np.where(ink.any(axis=0))[0]
    return [int(cols.min()), int(rows.min()), int(cols.max())+1, int(rows.max())+1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True); ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", required=True); ap.add_argument("--caption", default="图版")
    ap.add_argument("--dry", action="store_true"); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--preview-dir", default="_工作记录/补图_20261126/预览/plates")
    args = ap.parse_args()
    if not args.dry and not args.apply: print("需 --dry 或 --apply"); return 2
    import fitz
    bj = os.path.join(args.book, "book.json")
    book = json.load(open(bj, encoding="utf-8"))
    pages = parse_pages(args.pages)
    have = {(b.get("page") or 0) for b in book["blocks"] if b.get("type") == "figure"}
    pages = [n for n in pages if n not in have]
    doc = fitz.open(args.pdf)
    # 渲染宽：用 OCR 坐标 p99（与既有 box 同空间）
    xs = []
    pdir = os.path.join(args.book, "pages")
    for fn in os.listdir(pdir):
        if not fn.endswith(("_ocr2.json", "_text.json")): continue
        d = json.load(open(os.path.join(pdir, fn), encoding="utf-8"))
        if isinstance(d, list): xs.extend([l.get("x1") or 0 for l in d])
    xs.sort(); target_w = int(round(xs[int(len(xs)*0.99)])) if xs else 1200
    os.makedirs(args.preview_dir, exist_ok=True)
    figs_dir = os.path.join(args.book, "assets"); os.makedirs(figs_dir, exist_ok=True)
    next_no = 1
    for fn in os.listdir(figs_dir):
        m = re.match(r"fig-(\d+)\.webp$", fn)
        if m: next_no = max(next_no, int(m.group(1)) + 1)
    items = []
    for n in pages:
        pg = doc[n-1]; scale = target_w / pg.rect.width
        pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        bb = ink_bbox(img)
        if not bb: continue
        x0, y0, x1, y1 = bb
        crop = img.crop((max(0,x0-PAD), max(0,y0-PAD), min(img.size[0],x1+PAD), min(img.size[1],y1+PAD)))
        prev = os.path.join(args.preview_dir, "p%03d.png" % n); crop.save(prev)
        items.append({"page": n, "box": [max(0,x0-PAD), max(0,y0-PAD), min(img.size[0],x1+PAD), min(img.size[1],y1+PAD)], "prev": prev})
    doc.close()
    print("待补页数 %d" % len(items))
    for it in items: print("  p%d box=%s" % (it["page"], it["box"]))
    if args.dry:
        print("DRY-RUN（预览在 %s）" % args.preview_dir); return 0

    bak = os.path.join(args.book, "book.json.bak_%s_补图版前" % now_tag())
    shutil.copy2(bj, bak); print("备份 %s" % os.path.basename(bak))
    blocks = book["blocks"]; added = []
    for it in items:
        clean = Image.open(it["prev"]).convert("RGB")
        fid = "fig-%03d" % next_no; next_no += 1
        clean.save(os.path.join(figs_dir, fid + ".webp"), quality=90)
        blk = {"type": "figure", "id": fid, "src": "assets/%s.webp" % fid,
               "page": it["page"], "y": it["box"][1], "box": list(it["box"]),
               "caption": args.caption, "originalCaption": None,
               "displayCaption": args.caption, "number": next_no - 1, "plate": True}
        idxs = [i for i, b in enumerate(blocks) if (b.get("page") or 0) == it["page"]]
        pos = idxs[-1] + 1 if idxs else len(blocks)
        if not idxs:
            for i, b in enumerate(blocks):
                if (b.get("page") or 0) > it["page"]: pos = i; break
        blocks.insert(pos, blk); added.append(blk)
    figs = [b for b in blocks if b.get("type") == "figure"]
    paired = sum(1 for b in figs if (b.get("caption") or "").strip())
    book.setdefault("stats", {})["figures"] = len(figs); book["stats"]["paired"] = paired
    for a in added:
        assert any(b.get("id") == a["id"] for b in blocks); assert os.path.exists(os.path.join(args.book, a["src"]))
    json.dump(book, open(bj, "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=1)
    print("已写回：新增 %d 张，figures=%d paired=%d" % (len(added), len(figs), paired))
    return 0

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8"); sys.exit(main())
