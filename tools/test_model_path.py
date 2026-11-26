# -*- coding: utf-8 -*-
"""tools/vertical_ocr.py 的模型路径解析单测（工程 V1.2 · 任务 P2-02）。

覆盖 resolve_model_path 的 4 级优先级、全缺报错文案、相对/绝对路径、cwd 无关性，
以及 ModelNotFound 向后兼容（旧 except FileNotFoundError 仍成立）。

纯路径逻辑：resolve_model_path 只做 Path.is_file()，不加载 onnx、不 import 推理依赖。
用法：
  python tools/test_model_path.py            # 或 python -X utf8 tools/test_model_path.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import vertical_ocr as VO  # noqa: E402

FN = "ch_PP-OCRv5_rec_mobile.onnx"
ENV_KEY = "VERTICAL_OCR_MODEL"


class ModelPathCase(unittest.TestCase):
    """基类：把两级默认目录指向临时树、隔离 env 与 cwd，跑完原样还原。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="vmodel_path_"))
        # 模拟「项目根/models」与「tools/models」两级布局
        self.root_models = self._tmp / "root" / "models"
        self.tools_models = self._tmp / "root" / "tools" / "models"
        self.root_models.mkdir(parents=True)
        self.tools_models.mkdir(parents=True)

        # 备份并接管模块级路径常量
        self._saved_mod = {n: getattr(VO, n) for n in
                           ("ROOT_MODELS_DIR", "TOOLS_MODELS_DIR", "MODEL_FILENAME")}
        VO.ROOT_MODELS_DIR = self.root_models
        VO.TOOLS_MODELS_DIR = self.tools_models
        VO.MODEL_FILENAME = FN

        # 隔离环境变量
        self._saved_env = os.environ.get(ENV_KEY, "__unset__")
        os.environ.pop(ENV_KEY, None)
        # 隔离 cwd
        self._saved_cwd = os.getcwd()

    def tearDown(self):
        for n, v in self._saved_mod.items():
            setattr(VO, n, v)
        if self._saved_env == "__unset__":
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self._saved_env
        os.chdir(self._saved_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    # -- 工具 --------------------------------------------------------
    def place(self, d: Path, name: str = FN) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_bytes(b"fake-onnx-bytes-for-path-test-only")   # 内容无所谓，只要 is_file()
        return f


class TestPriority(ModelPathCase):
    def test_search_order_locks_root_before_tools(self):
        """③ 项目根 必须排在 ④ tools 之前（防止回归成只认老布局）。"""
        self.assertEqual(VO._default_model_search_paths(),
                         [self.root_models / FN, self.tools_models / FN])

    def test_1_explicit_beats_all(self):
        """① 显式(--vmodel) 最高：即便 env + root + tools 都在，也听显式。"""
        chosen = self.place(self._tmp / "custom")
        self.place(self.tools_models)
        self.place(self.root_models)
        os.environ[ENV_KEY] = str(self.place(self._tmp / "envdir"))
        got = VO.resolve_model_path(str(chosen))
        self.assertEqual(Path(got), chosen)

    def test_2_env_beats_defaults(self):
        """② 环境变量 高于 ③④ 默认（无显式时）。"""
        envf = self.place(self._tmp / "envdir")
        self.place(self.tools_models)
        self.place(self.root_models)
        os.environ[ENV_KEY] = str(envf)
        got = VO.resolve_model_path()                 # 无 explicit
        self.assertEqual(Path(got), envf)

    def test_3_root_beats_tools(self):
        """③ 项目根 命中时优先于 ④ tools（文档推荐位置生效）。"""
        rootf = self.place(self.root_models)
        self.place(self.tools_models)
        got = VO.resolve_model_path()
        self.assertEqual(Path(got), rootf)

    def test_4_tools_fallback_when_root_absent(self):
        """④ 老布局兼容：根目录缺、tools 有 → 仍能用 tools/models（修复前的历史路径）。"""
        toolsf = self.place(self.tools_models)
        got = VO.resolve_model_path()
        self.assertEqual(Path(got), toolsf)


class TestErrors(ModelPathCase):
    def test_all_missing_raises_with_paths_and_guidance(self):
        """全缺 → ModelNotFound，文案含全部搜索路径 + 二选一修复指引。"""
        with self.assertRaises(VO.ModelNotFound) as ctx:
            VO.resolve_model_path()
        msg = str(ctx.exception)
        # 列出 ③④ 两个真实搜索路径
        self.assertIn(str(self.root_models / FN), msg)
        self.assertIn(str(self.tools_models / FN), msg)
        # 修复指引：env 与放置位置二选一
        self.assertIn(ENV_KEY, msg)
        self.assertIn("--vmodel", msg)
        self.assertIn(FN, msg)

    def test_explicit_invalid_no_silent_fallback(self):
        """① 给了但无效 → 报错，绝不静默改用 tools 默认（否则会用错模型出乱码）。"""
        self.place(self.tools_models)                    # 默认本可用
        bad = self._tmp / "does_not_exist.onnx"
        with self.assertRaises(VO.ModelNotFound) as ctx:
            VO.resolve_model_path(str(bad))
        self.assertIn(str(bad), str(ctx.exception))

    def test_env_invalid_no_silent_fallback(self):
        """② env 设了但无效 → 报错，不静默回落到默认。"""
        self.place(self.tools_models)
        bad = self._tmp / "env_missing.onnx"
        os.environ[ENV_KEY] = str(bad)
        with self.assertRaises(VO.ModelNotFound) as ctx:
            VO.resolve_model_path()
        msg = str(ctx.exception)
        self.assertIn(str(bad), msg)
        self.assertIn(ENV_KEY, msg)

    def test_model_not_found_is_filenotfounderror(self):
        """向后兼容：旧代码 except FileNotFoundError 仍能接住新异常。"""
        self.assertTrue(issubclass(VO.ModelNotFound, FileNotFoundError))
        with self.assertRaises(FileNotFoundError):
            VO.resolve_model_path()

    def test_resolve_model_alias_matches(self):
        """兼容别名 resolve_model 行为与 resolve_model_path 一致。"""
        rootf = self.place(self.root_models)
        self.assertEqual(VO.resolve_model(), rootf)
        self.assertEqual(VO.resolve_model_path(), rootf)


class TestRelativeAndCwd(ModelPathCase):
    def test_relative_explicit_resolves_against_cwd(self):
        """相对 --vmodel 按当前 cwd 解析：换目录结果随之变化（符合直觉）。"""
        sub = self._tmp / "sub"
        f = self.place(sub, name="custom_rec.onnx")
        os.chdir(sub)
        got = VO.resolve_model_path("custom_rec.onnx")
        self.assertEqual(Path(got), Path("custom_rec.onnx"))
        self.assertTrue(Path(got).is_file())
        # 从别处启动，同一相对串应命中不到 → 报错（不静默回落）
        os.chdir(self._tmp)
        with self.assertRaises(VO.ModelNotFound):
            VO.resolve_model_path("custom_rec.onnx")

    def test_absolute_explicit_cwd_independent(self):
        """绝对 --vmodel：任意 cwd 下都命中同一文件。"""
        f = self.place(self._tmp / "abs", name="abs_rec.onnx")
        for here in (self._tmp, self._tmp / "abs", self.tools_models):
            os.chdir(here)
            self.assertEqual(Path(VO.resolve_model_path(str(f))), f)

    def test_default_resolution_cwd_independent(self):
        """③④ 默认解析锚定模块位置(HERE)，与进程 cwd 无关，且返回绝对路径。"""
        toolsf = self.place(self.tools_models)           # 只放 tools，走 ④ 兼容路径
        others = [self._tmp, self.root_models, Path(tempfile.gettempdir())]
        results = []
        for here in others:
            os.chdir(here)
            r = VO.resolve_model_path()
            results.append(Path(r))
            self.assertTrue(Path(r).is_absolute(), "默认解析必须返回绝对路径：%s" % r)
        self.assertTrue(all(x == results[0] for x in results),
                        "不同 cwd 启动结果必须一致：%s" % results)
        self.assertEqual(results[0], toolsf)


@unittest.skipUnless(
    (TOOLS / "models" / FN).is_file() or (TOOLS.parent / "models" / FN).is_file(),
    "本机未安装竖排 rec 模型，跳过真实默认路径校验")
class TestRealDefault(unittest.TestCase):
    """用真实布局回归 P2-02 现场：不动模块目录常量，仅隔离 env/cwd，跑完原样还原。

    本机现状：tools/models/ 有 onnx、项目根 models/ 没有 → 修复后应命中 ④ 老布局。
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="vmodel_real_"))
        self._saved_env = os.environ.get(ENV_KEY, "__unset__")
        os.environ.pop(ENV_KEY, None)
        self._saved_cwd = os.getcwd()
        self._saved_root = VO.ROOT_MODELS_DIR

    def tearDown(self):
        VO.ROOT_MODELS_DIR = self._saved_root
        if self._saved_env == "__unset__":
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = self._saved_env
        os.chdir(self._saved_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_real_default_absolute_and_cwd_independent(self):
        first = VO.resolve_model_path()
        self.assertTrue(Path(first).is_absolute())
        self.assertTrue(Path(first).is_file())
        for here in (TOOLS, TOOLS.parent, Path(tempfile.gettempdir())):
            os.chdir(here)
            self.assertEqual(VO.resolve_model_path(), first)

    def test_real_tools_layout_still_works(self):
        """关键回归：项目根/models 缺失时，老 tools/models 仍须命中（两级兼容）。"""
        if not (TOOLS / "models" / FN).is_file():
            self.skipTest("本机无 tools/models 老布局（仅根目录装了模型）")
        VO.ROOT_MODELS_DIR = self._tmp / "no_root_models"      # 假装根目录没有
        r = VO.resolve_model_path()
        self.assertEqual(Path(r), TOOLS / "models" / FN)       # 必须回落到 ④


if __name__ == "__main__":
    unittest.main(verbosity=2)