# -*- coding: utf-8 -*-
"""题目↔原文对账

兼容入口：等价于 python tools/qbank.py audit ...（保留旧文件名，别处脚本不受影响）
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qbank import main

if __name__ == '__main__':
    sys.exit(main(['audit'] + sys.argv[1:]))
