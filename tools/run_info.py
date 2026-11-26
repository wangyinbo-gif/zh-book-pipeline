# -*- coding: utf-8 -*-
"""P1-02 配套：run_info 落盘模块（独立，不改 pipeline.py）。

用途：为每本书稿留一份「这一版结果是谁在什么环境跑出来的」指纹档案，
出问题时能回答：哪个 commit、哪套依赖、哪个输入文件、什么参数、什么系统。

主入口：
    from run_info import write_run_info
    write_run_info(book_dir, stage, config)
      book_dir : 书稿目录（run_info.json 落这里）
      stage    : 阶段名（如 "convert"/"post"/"e2e_ci"；lead 挂线时按 pipeline 阶段传）
      config   : dict，可选键：
                 input   —— 输入文件路径（算 SHA-256）
                 params  —— 本次运行的参数 dict（原样记录）
                 extra   —— 任意附加字段
                 root    —— git 仓库根（默认取本文件父目录的父目录）
返回 run_info.json 路径；任何采集失败都降级为 None/占位，绝不抛异常打断主管线。

run_info.json 结构：
  {"version":1, "entries":[ {stage, at, git, python, deps, input, params, os}, ... ]}
  entries 按追加顺序保留最近 MAX_ENTRIES 条。

lead 挂线示例（pipeline.py 内，不改本模块）：
    # _run_stages 里每个阶段 done[st] 记录之后：
    try:
        import run_info
        run_info.write_run_info(book_dir, st, {"input": pdf,
            "params": {"scale": args.scale, "vertical": vertical}})
    except Exception:
        pass
CLI：python tools/run_info.py <book_dir> --stage convert --input a.pdf [--param k=v ...]
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(HERE)
MAX_ENTRIES = 20
DEP_NAMES = ["pillow", "numpy", "pypdfium2", "pymupdf", "rapidocr-onnxruntime",
             "onnxruntime", "opencv-python", "opencv-python-headless",
             "pyclipper", "shapely"]


def file_sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        return h.hexdigest()
    except Exception:
        return None


def collect_git(root):
    """git commit/branch/dirty；任何一步失败给 None（CI 浅克隆也拿得到 commit）。"""
    info = {"commit": None, "branch": None, "dirty": None, "root": root}
    def _git(*a):
        try:
            r = subprocess.run(["git", "-C", root] + list(a), capture_output=True,
                               text=True, timeout=10, encoding="utf-8", errors="replace")
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
    info["commit"] = _git("rev-parse", "HEAD")
    info["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
    st = _git("status", "--porcelain")
    if st is not None:
        info["dirty"] = bool(st)
    return info


def collect_deps():
    """importlib.metadata 读依赖版本；未装的记 None。"""
    from importlib import metadata
    out = {}
    for name in DEP_NAMES:
        try:
            out[name] = metadata.version(name)
        except Exception:
            out[name] = None
    return out


def collect_os():
    return {"system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "platform": platform.platform(),
            "cpu_count": (os.cpu_count() or 0)}


def build_entry(stage, config=None):
    config = config or {}
    inp = config.get("input")
    entry = {
        "stage": stage,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "git": collect_git(config.get("root") or DEFAULT_ROOT),
        "python": sys.version.split()[0],
        "deps": collect_deps(),
        "input": ({"path": str(inp), "sha256": file_sha256(str(inp)),
                   "bytes": (os.path.getsize(str(inp)) if inp and os.path.exists(str(inp)) else None)}
                  if inp else None),
        "params": config.get("params") or {},
        "os": collect_os(),
    }
    if config.get("extra"):
        entry["extra"] = config["extra"]
    return entry


def write_run_info(book_dir, stage, config=None):
    """追加一条 stage 档案到 <book_dir>/run_info.json，返回路径。"""
    book_dir = os.path.abspath(book_dir)
    os.makedirs(book_dir, exist_ok=True)
    path = os.path.join(book_dir, "run_info.json")
    data = None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    if not isinstance(data, dict) or data.get("version") != 1:
        data = {"version": 1, "entries": []}
    entry = build_entry(stage, config)
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
    data["last_stage"] = stage
    data["updated_at"] = entry["at"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="写 run_info.json（书稿运行档案）")
    ap.add_argument("book_dir")
    ap.add_argument("--stage", default="manual")
    ap.add_argument("--input", default="")
    ap.add_argument("--param", action="append", default=[],
                    help="k=v，可多次；v 尝试按 JSON 解析")
    args = ap.parse_args(argv)
    params = {}
    for kv in args.param:
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                params[k] = json.loads(v)
            except Exception:
                params[k] = v
    p = write_run_info(args.book_dir, args.stage,
                       {"input": args.input or None, "params": params})
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
