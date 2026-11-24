"""Keep the original blocks and source assets when cleanup detaches figures."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def record_removal(book_dir, stage, blocks, details):
    """Save explicit receipts before rewriting the manuscript."""
    book_dir = Path(book_dir)
    directory = book_dir / "figure-cleanup"
    directory.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{timestamp}-{stage}-{uuid4().hex[:8]}.json"
    receipt = {
        "stage": stage,
        "before_book_sha256": hashlib.sha256((book_dir / "book.json").read_bytes()).hexdigest(),
        "removed_blocks": blocks,
        "details": details,
    }
    payload = json.dumps(receipt, ensure_ascii=False, indent=2)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    return path
