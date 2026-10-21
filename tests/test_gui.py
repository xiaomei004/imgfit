"""图形界面冒烟测试：能在没有显示器的环境下自动跳过。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from helpers import photo, save  # noqa: E402


def _tk_root():
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        return root
    except Exception:
        return None


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.root = _tk_root()
        if self.root is None:
            self.skipTest("当前环境没有可用的图形界面")
        from imgfit.gui import ImgFitApp

        self.app = ImgFitApp(self.root)

    def tearDown(self):
        if getattr(self, "root", None) is not None:
            self.root.destroy()

    def test_defaults(self):
        self.assertEqual(self.app.budget_var.get(), "200kb")
        self.assertIn("分辨率优先", self.app.mode_var.get())

    def test_options_parsed_from_widgets(self):
        self.app.budget_var.set("300kb")
        options = self.app._options()
        self.assertEqual(options.budget, 300 * 1024)
        self.assertEqual(options.mode, "resolution")

    def test_bad_budget_raises(self):
        from imgfit.size import SizeError

        self.app.budget_var.set("lots")
        with self.assertRaises(SizeError):
            self.app._options()

    def test_refresh_lists_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = save(photo((300, 200)), Path(tmp.name) / "a.jpg", quality=70)
        self.app.files = [path]
        self.app._refresh_files()
        self.assertEqual(len(self.app.file_tree.get_children()), 1)
        self.assertIn("1", self.app.count_label.cget("text"))

    def test_running_state_toggles_buttons(self):
        self.app._set_running(True)
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "normal")
        self.app._set_running(False)
        self.assertEqual(str(self.app.start_button.cget("state")), "normal")

    def test_insert_result_rows(self):
        from imgfit.engine import ACTION_COPIED, FileResult

        result = FileResult(
            source=Path("x.jpg"),
            target=Path("out/x.jpg"),
            original_bytes=1024,
            output_bytes=1024,
            original_size=(100, 80),
            output_size=(100, 80),
            action=ACTION_COPIED,
        )
        self.app._insert_result(result)
        self.assertEqual(len(self.app.result_tree.get_children()), 1)

    def test_insert_failed_row(self):
        from imgfit.engine import ACTION_FAILED, FileResult

        result = FileResult(
            source=Path("bad.jpg"),
            target=None,
            original_bytes=2048,
            action=ACTION_FAILED,
            detail="UnidentifiedImageError: 无法识别",
        )
        self.app._insert_result(result)
        row = self.app.result_tree.get_children()[0]
        values = self.app.result_tree.item(row, "values")
        self.assertEqual(values[4], "失败")

    def test_mode_mapping_to_quality(self):
        from imgfit.engine import MODE_QUALITY

        self.app.mode_var.set("质量优先（照片 / 素材）")
        self.assertEqual(self.app._options().mode, MODE_QUALITY)

    def test_buttons_disabled_before_any_run(self):
        # 没有报告时，"打开输出目录/导出" 应该是灰的
        self.assertEqual(str(self.app.open_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.export_button.cget("state")), "disabled")
        self.app._open_output()   # 不应抛异常
        self.app._export()        # 不应弹对话框、不应抛异常


if __name__ == "__main__":
    unittest.main()
