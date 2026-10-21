"""批量编排：文件收集、预算分配、执行与报告。"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))



from helpers import document, noise, photo, save  # noqa: E402
from imgfit.pipeline import (  # noqa: E402
    BatchOptions,
    BudgetError,
    allocate_budgets,
    collect_inputs,
    default_output_dir,
    plan_batch,
    run_batch,
    write_report,
)


class CollectInputsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_recursive_picks_images_only(self):
        save(photo((200, 150)), self.root / "a.jpg")
        (self.root / "sub").mkdir()
        save(photo((200, 150)), self.root / "sub" / "b.png")
        (self.root / "sub" / "note.txt").write_text("x", encoding="utf-8")
        found = collect_inputs([self.root])
        self.assertEqual([p.name for p in found], ["a.jpg", "b.png"])

    def test_non_recursive(self):
        save(photo((200, 150)), self.root / "a.jpg")
        (self.root / "sub").mkdir()
        save(photo((200, 150)), self.root / "sub" / "b.jpg")
        found = collect_inputs([self.root], recursive=False)
        self.assertEqual([p.name for p in found], ["a.jpg"])

    def test_output_dir_excluded(self):
        save(photo((200, 150)), self.root / "a.jpg")
        out = self.root / "压缩"
        save(photo((200, 150)), out / "old.jpg")
        found = collect_inputs([self.root], exclude_dirs=[out])
        self.assertEqual([p.name for p in found], ["a.jpg"])

    def test_duplicates_removed(self):
        save(photo((200, 150)), self.root / "a.jpg")
        found = collect_inputs([self.root, self.root / "a.jpg"])
        self.assertEqual(len(found), 1)

    def test_missing_path_ignored(self):
        self.assertEqual(collect_inputs([self.root / "nope"]), [])

    def test_default_output_dir_for_folder(self):
        self.assertEqual(default_output_dir([self.root]), self.root / "压缩")

    def test_default_output_dir_for_files(self):
        save(photo((200, 150)), self.root / "a.jpg")
        save(photo((200, 150)), self.root / "b.jpg")
        self.assertEqual(
            default_output_dir([self.root / "a.jpg", self.root / "b.jpg"]).name, "压缩"
        )


class AllocateBudgetsTests(unittest.TestCase):
    def test_small_files_only_take_what_they_need(self):
        sizes = [100, 10 ** 7, 10 ** 7]
        allocation = allocate_budgets(sizes, total=1024 * 1024, minimum=1024)
        self.assertEqual(allocation[0], 100)
        self.assertGreater(allocation[1], allocation[0])

    def test_total_is_respected(self):
        sizes = [10 ** 6] * 9
        total = 2 * 1024 * 1024
        allocation = allocate_budgets(sizes, total=total, minimum=4096)
        self.assertLessEqual(sum(allocation), total)
        self.assertEqual(len(allocation), 9)

    def test_every_file_gets_something(self):
        sizes = [500 * 1024] * 4
        allocation = allocate_budgets(sizes, total=200 * 1024, minimum=1024)
        self.assertTrue(all(value >= 1024 for value in allocation))

    def test_raises_when_total_too_small(self):
        with self.assertRaises(BudgetError):
            allocate_budgets([1000, 1000, 1000], total=500, minimum=1024)

    def test_empty(self):
        self.assertEqual(allocate_budgets([], total=1000), [])


class RunBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "材料"
        self.out = self.root / "压缩"
        self.inbox.mkdir()

    def _options(self, **kwargs) -> BatchOptions:
        base = dict(output_dir=self.out, budget=120 * 1024, max_dim=1200, floor_dim=400, dim_step=100)
        base.update(kwargs)
        return BatchOptions(**base)

    def test_all_results_within_budget(self):
        save(document((1000, 1300)), self.inbox / "成绩单.jpg", quality=95)
        save(noise((1200, 900)), self.inbox / "证书.jpg", quality=95)
        save(photo((800, 600)), self.inbox / "小图.jpg", quality=80)
        report = run_batch([self.inbox], self._options())
        self.assertEqual(len(report.results), 3)
        self.assertFalse(report.failed)
        self.assertEqual(report.over_budget, [])
        for result in report.results:
            self.assertLessEqual(result.output_bytes, 120 * 1024)
            self.assertTrue(Path(result.target).exists())

    def test_small_file_is_copied(self):
        save(photo((300, 200)), self.inbox / "tiny.jpg", quality=70)
        report = run_batch([self.inbox], self._options(budget=200 * 1024))
        self.assertEqual(len(report.copied), 1)
        self.assertEqual(len(report.compressed), 0)

    def test_total_budget_mode(self):
        for index in range(3):
            save(noise((1000, 800), seed=index), self.inbox / f"n{index}.jpg", quality=95)
        total = 240 * 1024
        report = run_batch([self.inbox], self._options(budget=None, total_budget=total))
        self.assertFalse(report.failed)
        self.assertLessEqual(report.total_output_bytes, total)

    def test_failure_is_isolated(self):
        save(document((900, 1200)), self.inbox / "good.jpg", quality=90)
        (self.inbox / "broken.jpg").write_bytes(b"not an image" * 50_000)
        report = run_batch([self.inbox], self._options())
        self.assertEqual(len(report.failed), 1)
        self.assertEqual(len(report.compressed) + len(report.copied), 1)

    def test_progress_callback_reports_every_file(self):
        for index in range(3):
            save(photo((500, 400), ), self.inbox / f"p{index}.jpg", quality=80)
        seen = []
        run_batch([self.inbox], self._options(), progress=lambda done, total, res: seen.append((done, total)))
        self.assertEqual([item[0] for item in seen], [1, 2, 3])
        self.assertEqual(seen[-1][1], 3)

    def test_cancel_stops_early(self):
        for index in range(5):
            save(noise((900, 700), seed=index), self.inbox / f"c{index}.jpg", quality=95)
        report = run_batch([self.inbox], self._options(), should_cancel=lambda: True)
        self.assertEqual(report.results, [])

    def test_empty_input(self):
        report = run_batch([self.inbox], self._options())
        self.assertEqual(report.results, [])

    def test_plan_batch_marks_copies(self):
        save(photo((300, 200)), self.inbox / "tiny.jpg", quality=70)
        save(noise((1500, 1000)), self.inbox / "big.jpg", quality=95)
        items = plan_batch([self.inbox], self._options(budget=100 * 1024))
        actions = {item.source.name: item.action for item in items}
        self.assertEqual(actions["tiny.jpg"], "copy")
        self.assertEqual(actions["big.jpg"], "compress")

    def test_write_csv_report(self):
        save(document((800, 1000)), self.inbox / "a.jpg", quality=90)
        report = run_batch([self.inbox], self._options())
        path = write_report(report, self.root / "报告.csv")
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["文件名"], "a.jpg")
        self.assertIn("处理方式", rows[0])

    def test_write_json_report(self):
        save(document((800, 1000)), self.inbox / "a.jpg", quality=90)
        report = run_batch([self.inbox], self._options())
        path = write_report(report, self.root / "报告.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["成功"], 1)
        self.assertEqual(len(payload["明细"]), 1)


if __name__ == "__main__":
    unittest.main()
