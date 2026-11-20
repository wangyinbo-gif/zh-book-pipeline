# -*- coding: utf-8 -*-
"""集中路径配置。

设置环境变量 BOOKROOT 指向书库根目录（其下有 books/、题库/、data/、inbox/）；
未设置时默认取本文件所在目录的上一级（即仓库根）。
"""
import os

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("BOOKROOT") or os.path.dirname(TOOLS)
BOOKS = os.path.join(ROOT, "books")
QBANK = os.path.join(ROOT, "题库")
INBOX = os.path.join(ROOT, "inbox")
