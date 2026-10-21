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


if __name__ == "__main__":
    unittest.main()
