"""尺寸解析与格式化。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from imgfit.size import SizeError, format_size, parse_size  # noqa: E402


class ParseSizeTests(unittest.TestCase):
    def test_binary_units(self):
        self.assertEqual(parse_size("200kb"), 200 * 1024)
        self.assertEqual(parse_size("200KB"), 200 * 1024)
        self.assertEqual(parse_size("200K"), 200 * 1024)
        self.assertEqual(parse_size("1.5mb"), int(1.5 * 1024 * 1024))
        self.assertEqual(parse_size("1g"), 1024 ** 3)

    def test_plain_numbers_are_bytes(self):
        self.assertEqual(parse_size("204800"), 204800)
        self.assertEqual(parse_size(204800), 204800)
        self.assertEqual(parse_size(1.6), 2)

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_size("  200 kb  "), 200 * 1024)

    def test_invalid(self):
        for bad in ("", "abc", "200tb", "-5kb", "0", "0kb", "1..5mb"):
            with self.subTest(bad=bad):
                with self.assertRaises(SizeError):
                    parse_size(bad)

    def test_bool_rejected(self):
        with self.assertRaises(SizeError):
            parse_size(True)


class FormatSizeTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(204800), "200.0 KB")
        self.assertEqual(format_size(1024 ** 2 * 3 // 2), "1.5 MB")

    def test_negative(self):
        with self.assertRaises(SizeError):
            format_size(-1)


if __name__ == "__main__":
    unittest.main()
