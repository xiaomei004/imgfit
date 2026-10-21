"""覆盖率徽章脚本：解析 coverage.xml、生成 SVG、按颜色分档。"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import coverage_badge  # noqa: E402


def quiet_main(args):
    """跑一次脚本但吞掉它的打印，保持测试输出干净。"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return coverage_badge.main(args)


class CoverageBadgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _xml(self, line_rate: str) -> Path:
        path = self.dir / "coverage.xml"
        path.write_text(
            f'<?xml version="1.0"?>\n<coverage line-rate="{line_rate}" branch-rate="0" version="7.0">'
            "</coverage>",
            encoding="utf-8",
        )
        return path

    def test_badge_contains_percentage(self):
        out = self.dir / "coverage.svg"
        code = quiet_main(["--xml", str(self._xml("0.876")), "--out", str(out)])
        self.assertEqual(code, 0)
        svg = out.read_text(encoding="utf-8")
        self.assertIn("88%", svg)
        ET.fromstring(svg)  # 必须是合法 XML

    def test_color_thresholds(self):
        self.assertEqual(coverage_badge.pick_color(95), "#4c1")
        self.assertEqual(coverage_badge.pick_color(85), "#97ca00")
        self.assertEqual(coverage_badge.pick_color(65), "#fe7d37")
        self.assertEqual(coverage_badge.pick_color(10), "#e05d44")

    def test_fail_under(self):
        out = self.dir / "b.svg"
        code = quiet_main(["--xml", str(self._xml("0.50")), "--out", str(out), "--fail-under", "80"])
        self.assertEqual(code, 1)

    def test_missing_xml_returns_two(self):
        code = quiet_main(["--xml", str(self.dir / "nope.xml"), "--out", str(self.dir / "x.svg")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
