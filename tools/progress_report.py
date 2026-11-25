import glob, json, os, sys, time
from paths import ROOT
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BOOKS = os.path.join(ROOT, "books")
STATE = os.path.join(BOOKS, "已汇报_v2.json")
START = os.path.join(BOOKS, "本批开始时间.txt")
sys.path.insert(0, os.path.join(ROOT, "tools"))

def batch_start():
    try:
        return float(open(START, encoding="utf-8").read().strip())
    except Exception:
        return time.time()

def main():
    import pipeline
    start = batch_start()
    state = {}
    if os.path.exists(STATE):
        state = json.load(open(STATE, encoding="utf-8"))
    new = []
    for d in sorted(glob.glob(os.path.join(BOOKS, "*"))):
        if not os.path.isdir(d):
            continue
        rep = os.path.join(d, "转换报告.md")
        bj = os.path.join(d, "book.json")
        if not (os.path.exists(rep) and os.path.exists(bj)):
            continue
        name = os.path.basename(d)
        mt = os.path.getmtime(rep)
        if mt < start:
            continue
        if state.get(name, 0) >= mt:
            continue
        book = json.load(open(bj, encoding="utf-8"))
        figs = [b for b in book.get("blocks", []) if b.get("type") == "figure"]
        cap = sum(1 for f in figs if f.get("caption"))
        hard, body = pipeline.hard_splits(book)
        ok, checks = pipeline.gate(d)
        new.append({"name": name, "chars": book["stats"]["chars"], "pages": book["stats"]["pages"],
                    "chapters": len(book.get("chapters", [])), "figs": [cap, len(figs)],
                    "hard": [hard, body], "ok": ok, "failed": [c["name"] for c in checks if not c["ok"]]})
        state[name] = mt
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
    print("NEW_COUNT %d" % len(new))
    for n in new:
        print("NEW " + json.dumps(n, ensure_ascii=False))

main()