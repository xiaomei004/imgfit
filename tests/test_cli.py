"""命令行入口：参数解析、退出码、以及"真的把文件压出来了"。"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from helpers import document, noise, photo, save  # noqa: E402
from imgfit.cli import _auto_headroom, _parse_color, main  # noqa: E402


def run_cli(args):
    """跑一次 CLI，吞掉 stdout/stderr，返回 (退出码, 输出文本)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(args)
        except SystemExit as exc:  # argparse 的 --version / 错误分支
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue() + err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "材料"
        self.out = self.root / "压缩"
        self.inbox.mkdir()
        save(document((1000, 1300)), self.inbox / "成绩单.jpg", quality=95)
        save(noise((1400, 1000)), self.inbox / "证书.jpg", quality=95)
        save(photo((400, 300)), self.inbox / "小图.jpg", quality=75)

    def test_runs_and_meets_budget(self):
        code, output = run_cli([str(self.inbox), "-t", "200kb", "-o", str(self.out)])
        self.assertEqual(code, 0, output)
        produced = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(produced), 3)
        budget = 200 * 1024 - _auto_headroom(200 * 1024)
        for path in self.out.iterdir():
            self.assertLessEqual(path.stat().st_size, budget)

    def test_dry_run_writes_nothing(self):
        code, output = run_cli([str(self.inbox), "-o", str(self.out), "-n"])
        self.assertEqual(code, 0, output)
        self.assertFalse(self.out.exists())
        self.assertIn("重新编码", output)

    def test_version(self):
        code, output = run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("imgfit", output)

    def test_invalid_target_exits_two(self):
        code, _ = run_cli([str(self.inbox), "-t", "abc"])
        self.assertEqual(code, 2)

    def test_no_inputs_prints_help(self):
        code, output = run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("imgfit", output)

    def test_report_flag(self):
        report = self.root / "r.csv"
        code, _ = run_cli([str(self.inbox), "-o", str(self.out), "--report", str(report)])
        self.assertEqual(code, 0)
        self.assertTrue(report.exists())
        self.assertIn("成绩单.jpg", report.read_text(encoding="utf-8-sig"))

    def test_quiet_mode(self):
        code, output = run_cli([str(self.inbox), "-o", str(self.out), "-q"])
        self.assertEqual(code, 0)
        self.assertNotIn("[1/", output)

    def test_broken_file_gives_partial_exit_code(self):
        (self.inbox / "broken.jpg").write_bytes(b"x" * 400_000)
        code, output = run_cli([str(self.inbox), "-o", str(self.out)])
        self.assertEqual(code, 1, output)
        self.assertIn("失败", output)

    def test_empty_folder(self):
        empty = self.root / "空"
        empty.mkdir()
        code, output = run_cli([str(empty), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("没有找到", output)


class HelperTests(unittest.TestCase):
    def test_headroom_scales_with_target(self):
        self.assertEqual(_auto_headroom(200 * 1024), 5 * 1024)
        self.assertEqual(_auto_headroom(20 * 1024), 1024)

    def test_parse_color(self):
        self.assertEqual(_parse_color("white"), (255, 255, 255))
        self.assertEqual(_parse_color("#102030"), (16, 32, 48))
        with self.assertRaises(Exception):
            _parse_color("not-a-color")


if __name__ == "__main__":
    unittest.main()
