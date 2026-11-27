"""Review exact local page-surgery changes and refuse releases with unresolved books."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from pipeline import block_text, count_chars
from realign_chapters import detect_json_format, dump_json_preserving

ROOT = Path(__file__).resolve().parents[1]
HOLD_FILE = Path("_工作记录/20261127_1635_P0收口/待核轮次.json")
ROLES = ("note", "table", "heading", "caption", "figure")


def digest(data):
    return sha256(data).hexdigest()


def encoded_book(book, raw):
    layout = detect_json_format(raw)
    newline = layout["newline"].encode("ascii")
    bom = b"\xef\xbb\xbf"
    return ((bom if raw.startswith(bom) else b"") +
            dump_json_preserving(book, layout).encode("utf-8") +
            (newline if raw.endswith(newline) else b""))


def is_live(book_dir):
    folder = Path(book_dir).resolve()
    return folder.parent == (ROOT / "books").resolve()


def roles(book):
    counts = Counter(b.get("type") for b in book["blocks"])
    counts["caption"] += sum(b.get("type") == "figure" and bool((b.get("caption") or "").strip())
                             for b in book["blocks"])
    return {k: counts[k] for k in ROLES}


def metrics(book):
    counts = roles(book)
    return {"chars": count_chars(book), "chapters": len(book.get("chapters") or []),
            "figures": counts["figure"], "captions": counts["caption"]}


def body_characters(book):
    return Counter(ch for b in book["blocks"] if b["type"] not in ("figure", "caption")
                   for ch in block_text(b) if not ch.isspace())


def block_pages(block, book):
    listed = block.get("pages")
    candidates = [block.get("page"), *(listed if isinstance(listed, list) else [])]
    pages = {p for p in candidates if type(p) is int and p > 0}
    if not pages:
        raise ValueError(f"{book}: an affected block lacks source-page attribution")
    return pages


def describe(folder, raw, before, after, plan_bytes, retained):
    folder = Path(folder).resolve()
    touched = set()
    for idx,b in enumerate(before["blocks"]):
        if idx not in retained:
            touched.update(block_pages(b, folder.name))
    kept = set(retained.values())
    for idx,b in enumerate(after["blocks"]):
        if idx not in kept:
            touched.update(block_pages(b, folder.name))
    old, new = metrics(before), metrics(after)
    old_roles, new_roles = roles(before), roles(after)
    vanished = [r for r in ROLES if old_roles[r] and not new_roles[r]]
    if vanished or new_roles["heading"] < old_roles["heading"]:
        raise ValueError(f"role regression: {vanished or ['heading']}")
    return {"book":folder.name, "target":f"books/{folder.name}/book.json", "pages":sorted(touched),
            "before_sha256":digest(raw), "plan_sha256":digest(plan_bytes),
            "after_sha256":digest(encoded_book(after,raw)),
            "metric_deltas":{k:new[k]-old[k] for k in old},
            "removed_body":dict(body_characters(before)-body_characters(after))}


def require_approval(path, actual):
    if not path:
        raise ValueError("main-library writes require --approval with the exact plan and change receipt")
    receipt = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(receipt, dict):
        raise ValueError("invalid approval receipt")
    approved_by = receipt.get("approved_by")
    evidence = receipt.get("evidence")
    if not isinstance(approved_by, str) or not approved_by.strip() or not isinstance(evidence,list) or not evidence:
        raise ValueError("approval must identify the reviewer and source evidence")
    if {k:v for k,v in receipt.items() if k not in ("approved_by","evidence")} != actual:
        raise ValueError("approval does not match exact title, hashes, pages, counters or removed content")
    linked = set()
    for row in evidence:
        if not isinstance(row,dict) or set(row)!={"path","sha256","pages"} or not isinstance(row["pages"],list):
            raise ValueError("invalid source evidence declaration")
        p = (ROOT / row["path"]).resolve()
        if not p.is_relative_to(ROOT) or not p.is_file() or digest(p.read_bytes()) != row["sha256"]:
            raise ValueError("source evidence is missing or changed")
        if any(type(n) is not int or n not in actual["pages"] for n in row["pages"]):
            raise ValueError("evidence page outside the approved change")
        linked.update(row["pages"])
    if linked != set(actual["pages"]):
        raise ValueError("every affected source page needs evidence")


def release_holds(root=None):
    """A changed book with an unresolved round must not be included in a new full package."""
    root = root or ROOT
    data = json.loads((root / HOLD_FILE).read_text(encoding="utf-8-sig"))
    held = data.get("held")
    if not isinstance(held, list):
        raise ValueError("invalid release hold ledger")
    result = {}
    for row in held:
        name = row["book"]
        if (not isinstance(name,str) or Path(name).name!=name or name in result
                or not (root / "books" / name / "book.json").is_file()):
            raise ValueError(f"release hold book does not match a unique book directory: {name!r}")
        result[name] = row["status"]
    return result
