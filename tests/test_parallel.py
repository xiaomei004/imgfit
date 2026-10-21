"""并行路径的回归测试：``-j N`` 必须和 ``-j 1`` 产出完全一致。

这里故意用**子进程**跑 CLI，而不是在测试进程里直接调 ``run_batch(jobs=4)``：
Windows 的 spawn 模式会把主模块重新导入一次，而在 pytest/unittest 里
"主模块"是测试运行器本身，容易踩到递归执行的坑。走子进程既贴近真实用法，
又避免这类环境问题。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from helpers import document, noise, photo, save  # noqa: E402

ROOT = SRC.parent


def run_cli(args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "imgfit", *args],
        capture_output=True,
        text=True,
        # 必须显式指定编码：Windows CI 的默认 pipe 编码是 cp1252，
        # 而子进程输出的是 UTF-8 中文，按默认编码解码会直接抛 UnicodeDecodeError
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(ROOT),
        timeout=300,
    )
    result.output = (result.stdout or "") + (result.stderr or "")
    return result


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "材料"
        (self.inbox / "甲").mkdir(parents=True)
        (self.inbox / "乙").mkdir()
        # 两个同名但内容不同的文件：并行时最容易抢同一个输出名
        save(document((900, 1200)), self.inbox / "甲" / "证书.jpg", quality=90)
        save(noise((1000, 800)), self.inbox / "乙" / "证书.jpg", quality=90)
        # 一张本来就达标的图：走"按字节复制"分支
        save(photo((600, 400)), self.inbox / "小图.jpg", quality=70)

    def test_parallel_output_is_identical_to_sequential(self):
        out_seq = self.root / "j1"
        out_par = self.root / "j4"
        r1 = run_cli([str(self.inbox), "-t", "100kb", "-o", str(out_seq), "-q"])
        r4 = run_cli([str(self.inbox), "-t", "100kb", "-o", str(out_par), "-j", "2", "-q"])
        self.assertEqual(r1.returncode, 0, r1.output)
        self.assertEqual(r4.returncode, 0, r4.output)

        names_seq = sorted(p.name for p in out_seq.iterdir())
        names_par = sorted(p.name for p in out_par.iterdir())
        self.assertEqual(names_seq, names_par)
        self.assertEqual(len(names_seq), 3)
        for name in names_seq:
            self.assertEqual(
                (out_seq / name).read_bytes(),
                (out_par / name).read_bytes(),
                f"{name} 的字节内容不一致",
            )

    def test_duplicate_stems_are_disambiguated_by_folder(self):
        out = self.root / "out"
        result = run_cli([str(self.inbox), "-t", "100kb", "-o", str(out), "-q"])
        self.assertEqual(result.returncode, 0, result.output)
        names = sorted(p.name for p in out.iterdir())
        self.assertIn("甲_证书.jpg", names)
        self.assertIn("乙_证书.jpg", names)


if __name__ == "__main__":
    unittest.main()
