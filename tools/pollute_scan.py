#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全库夹带/书眉污染扫描 pollute_scan.py（只读）。
用法: python pollute_scan.py <books根> <输出json>
"""
import json, os, re, sys

AD = re.compile(u"幸福的味道|如果你不知道读什么书|免费送书|书伴|微信号|不同类型的书单"
                u"|得到电子书|QQ[：:]\\s*\\d{5,}|关注公众号|扫码|资源来自")
# 书眉串入：竖排古籍正文块头部「·」连缀人名+修/定/纂模式
MEI = re.compile(u"^[^\u3400-\u4dbf\u4e00-\u9fff]{0,4}(?:[\u4e00-\u9fff][\u00b7\u2022\uff65]){2,}[\u4e00-\u9fff]{0,2}(?:修|订|定|纂|撰|编)(?=[\u4e00-\u9fff]*[\u4e00-\u9fff])")
# 连续「X·」串≥2组+署名动词开头（竖排古籍式）
MEI2 = re.compile(u"^[\u4e00-\u9fff\uff1a:\u3000]{0,3}(?:[\u4e00-\u9fff]{1,3}[\u00b7\uff65]){2,}[\u4e00-\u9fff：:，。]{0,14}(?:修|订|定|纂|撰|编)")

def main():
    root, out = sys.argv[1], sys.argv[2]
    res = []
    for d in sorted(os.listdir(root)):
        bj = os.path.join(root, d, "book.json")
        if not os.path.isfile(bj):
            continue
        book = json.load(open(bj, encoding="utf-8"))
        for i, b in enumerate(book.get("blocks", [])):
            t = b.get("text") or ""
            hits = []
            m = AD.search(t)
            if m:
                # 提取广告段（从命中处到块尾或下一句号后）
                hits.append({"kind": "ad", "at": m.start(), "frag": t[m.start():m.start() + 60]})
            head = t[:40]
            if MEI2.match(head) or MEI.match(head):
                hits.append({"kind": "pagehead", "at": 0, "frag": head})
            for h in hits:
                res.append({"book": d[:20], "block": i, "page": b.get("page"),
                            "type": b.get("type"), "len": len(t), **h})
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    n_ad = len([r for r in res if r["kind"] == "ad"])
    n_ph = len([r for r in res if r["kind"] == "pagehead"])
    print("blocks=%d ad=%d pagehead=%d books=%d" % (len(res), n_ad, n_ph,
          len(set(r["book"] for r in res))))

if __name__ == "__main__":
    main()
