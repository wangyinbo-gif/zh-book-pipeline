# -*- coding: utf-8 -*-
"""题库结构校验

兼容入口：等价于 python tools/qbank.py check ...（保留旧文件名，别处脚本不受影响）
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qbank import main

if __name__ == '__main__':
    sys.exit(main(['check'] + sys.argv[1:]))
